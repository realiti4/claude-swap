# Auto-Switch Strategies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 5h hysteresis (anti-thrash), a transient-failure false-alarm fix, and a selectable `consume-first` auto-switch strategy to the menu bar.

**Architecture:** All in `src/claude_swap/menubar.py`. New pure, unit-tested functions (`next_blocked`, `_resets_at_ts`, `decide_consume_first`, `limiting_pct_by_account`, `evaluate_strategy`) plus a refactor of `decide_auto_switch` to take a `blocked` hysteresis set and report a distinct "unverifiable" outcome. A new `auto_switch_strategy` setting + a persisted `blocked` set in `MenuBarState`. Thin rumps glue dispatches on strategy, maintains `blocked`, and fetches all accounts for consume-first.

**Tech Stack:** Python 3.12+, stdlib `datetime`/`json`, `pytest`.

## Global Constraints

- `src/claude_swap/menubar.py` must import WITHOUT `rumps` (lazy `import rumps` inside `run()` only). All new pure code is stdlib-only.
- New pure functions are **total** — never raise (guard non-dict usage, missing windows, non-numeric pct, bad `resets_at`).
- `auto_switch_strategy` default is `"reactive"` (today's behavior unchanged unless the user opts in). Unknown strategy value → treat as `reactive`.
- Hysteresis is the fixed constant `AUTO_HYSTERESIS = 5.0` (not a setting).
- `consume-first` reuses the single `auto_switch_threshold` for both the 5h block threshold and the 7d cap.
- Decision-outcome strings (first tuple element), shared by both strategies: `"switch"`, `"none"`, `"unknown_active"`, `"no_candidate"` (notify), `"no_candidate_unverifiable"` (silent), `"all_session_limited"` (consume-first only, silent). `plan_auto_switch` already routes everything except `switch`/`no_candidate` to `noop`, so the silent outcomes need no plan change.
- End every commit with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

### Task 1: settings + state + constants

**Files:**
- Modify: `src/claude_swap/menubar.py`
- Test: `tests/test_menubar.py`

**Interfaces:**
- Produces: `MenuBarSettings.auto_switch_strategy: str = "reactive"`; constants `AUTO_STRATEGY_CHOICES = ("reactive", "consume-first")`, `AUTO_HYSTERESIS = 5.0`; `MenuBarState.blocked: list[str]` (persisted) with a generalized `load` that accepts a `list[str]`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_menubar.py
def test_settings_strategy_default():
    assert menubar.MenuBarSettings().auto_switch_strategy == "reactive"


def test_settings_strategy_round_trip(tmp_path: Path):
    path = tmp_path / "s.json"
    s = menubar.MenuBarSettings(auto_switch_strategy="consume-first")
    s.save(path)
    assert menubar.MenuBarSettings.load(path).auto_switch_strategy == "consume-first"


def test_state_blocked_round_trip(tmp_path: Path):
    path = tmp_path / "state.json"
    st = menubar.MenuBarState(last_switch_at=1.0, blocked=["2", "3"])
    st.save(path)
    loaded = menubar.MenuBarState.load(path)
    assert loaded.blocked == ["2", "3"]
    assert loaded.last_switch_at == 1.0


def test_state_blocked_defaults_when_malformed(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"blocked": [1, 2]}), encoding="utf-8")  # non-str elems
    assert menubar.MenuBarState.load(path).blocked == []
    path.write_text(json.dumps({"blocked": "nope"}), encoding="utf-8")
    assert menubar.MenuBarState.load(path).blocked == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_menubar.py -k "strategy or blocked" -v`
Expected: FAIL — `auto_switch_strategy` / `blocked` attributes don't exist.

- [ ] **Step 3: Implement**

In `src/claude_swap/menubar.py`:

(a) Add `field` to the dataclasses import:
```python
from dataclasses import asdict, dataclass, field, fields
```

(b) Add the constants after `AUTO_CHECK_CHOICES` (near the other `AUTO_*` constants):
```python
AUTO_STRATEGY_CHOICES: tuple[str, ...] = ("reactive", "consume-first")
AUTO_HYSTERESIS = 5.0  # dead band (percent) that prevents auto-switch thrash
```

(c) Add the settings field to `MenuBarSettings` (after `auto_switch_interval`):
```python
    auto_switch_strategy: str = "reactive"  # one of AUTO_STRATEGY_CHOICES
```

(d) Add the state field to `MenuBarState` (after `last_noswap_notify_at`):
```python
    blocked: list[str] = field(default_factory=list)  # 5h/limit-blocked account nums
```

(e) Generalize `MenuBarState.load`'s field loop to handle the list field:
```python
        kwargs = {}
        for f in fields(cls):
            default = getattr(defaults, f.name)
            val = raw.get(f.name)
            if isinstance(default, float):
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    kwargs[f.name] = float(val)
            elif isinstance(default, list):
                if isinstance(val, list) and all(isinstance(x, str) for x in val):
                    kwargs[f.name] = list(val)
        return cls(**kwargs)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_menubar.py -q`
Expected: PASS (new + all existing, including the existing `MenuBarState` timestamp tests under the generalized `load`).

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/menubar.py tests/test_menubar.py
git commit -m "feat(menubar): add auto_switch_strategy setting and blocked state

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: hysteresis FSM + reset-time parser

**Files:**
- Modify: `src/claude_swap/menubar.py`
- Test: `tests/test_menubar.py`

**Interfaces:**
- Consumes: `AUTO_HYSTERESIS` (Task 1).
- Produces:
  - `next_blocked(limiting_by_account: dict[str, float | None], threshold: float, hysteresis: float, prev_blocked) -> frozenset[str]`
  - `_resets_at_ts(window: dict | str | None) -> float`

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_menubar.py
def test_next_blocked_enter_stay_exit():
    prev = frozenset()
    # enter at >= threshold
    assert menubar.next_blocked({"1": 96.0}, 95, 5, prev) == frozenset({"1"})
    # stay blocked within the dead band (95-5=90 .. 95)
    assert menubar.next_blocked({"1": 92.0}, 95, 5, frozenset({"1"})) == frozenset({"1"})
    # exit only below threshold - hysteresis
    assert menubar.next_blocked({"1": 89.0}, 95, 5, frozenset({"1"})) == frozenset()
    # not blocked and below threshold -> stays out
    assert menubar.next_blocked({"1": 92.0}, 95, 5, frozenset()) == frozenset()


def test_next_blocked_unknown_carries_prev():
    assert menubar.next_blocked({"1": None}, 95, 5, frozenset({"1"})) == frozenset({"1"})
    assert menubar.next_blocked({"1": None}, 95, 5, frozenset()) == frozenset()


def test_resets_at_ts_orders_and_handles_missing():
    early = {"resets_at": "2026-06-24T07:00:00+00:00"}
    late = {"resets_at": "2026-06-26T07:00:00+00:00"}
    assert menubar._resets_at_ts(early) < menubar._resets_at_ts(late)
    assert menubar._resets_at_ts({"pct": 5.0}) == float("inf")   # no resets_at
    assert menubar._resets_at_ts({"resets_at": "garbage"}) == float("inf")
    assert menubar._resets_at_ts(None) == float("inf")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_menubar.py -k "next_blocked or resets_at" -v`
Expected: FAIL — functions don't exist.

- [ ] **Step 3: Implement**

In `src/claude_swap/menubar.py`, add `from datetime import datetime` to the imports (near `from pathlib import Path`). Then add, after `_worst_pct`:

```python
def next_blocked(
    limiting_by_account: dict[str, float | None],
    threshold: float,
    hysteresis: float,
    prev_blocked,
) -> frozenset[str]:
    """Sticky 'at-limit' set with a dead band, to stop auto-switch thrash.

    An account enters the set when its limiting % is ``>= threshold`` and leaves
    only when it drops below ``threshold - hysteresis``. Unknown (``None``) usage
    carries the prior membership — a network blip never unblocks an account.
    """
    nxt: set[str] = set()
    for num, pct in limiting_by_account.items():
        if pct is None:
            if num in prev_blocked:
                nxt.add(num)
            continue
        if num in prev_blocked:
            if pct >= threshold - hysteresis:
                nxt.add(num)
        elif pct >= threshold:
            nxt.add(num)
    return frozenset(nxt)


def _resets_at_ts(window: dict | str | None) -> float:
    """POSIX timestamp of a usage window's ``resets_at``; inf if missing/bad."""
    if isinstance(window, dict):
        ra = window.get("resets_at")
        if isinstance(ra, str):
            try:
                return datetime.fromisoformat(ra).timestamp()
            except ValueError:
                pass
    return float("inf")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_menubar.py -k "next_blocked or resets_at" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/menubar.py tests/test_menubar.py
git commit -m "feat(menubar): add hysteresis FSM and reset-time parser

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: hysteresis + unverifiable in `decide_auto_switch`

**Files:**
- Modify: `src/claude_swap/menubar.py`
- Test: `tests/test_menubar.py`

**Interfaces:**
- Consumes: `AUTO_HYSTERESIS`, `_worst_pct`, `_window_pct`.
- Produces: `decide_auto_switch(accounts, threshold, blocked=frozenset()) -> tuple[str, int | None]` — now hysteresis-aware and able to return `("no_candidate_unverifiable", None)`. `plan_auto_switch` is unchanged (silent outcomes already map to `noop`).

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_menubar.py
def _ra(num, pct5, pct7, active=False):
    return (num, f"a{num}@x.com", active,
            {"five_hour": {"pct": pct5}, "seven_day": {"pct": pct7}})


def test_decide_reactive_hysteresis_excludes_blocked_candidate():
    # active over limit; only peer (#2) is at 92 — within the 90..95 dead band.
    accts = [_ra(1, 99, 10, active=True), _ra(2, 92, 20)]
    # not blocked -> 92 < 95 -> eligible -> switch
    assert menubar.decide_auto_switch(accts, 95, frozenset()) == ("switch", 2)
    # blocked -> must clear 90 -> 92 >= 90 -> ineligible -> no candidate
    assert menubar.decide_auto_switch(accts, 95, frozenset({"2"})) == ("no_candidate", None)


def test_decide_reactive_unverifiable_when_only_peer_unreadable():
    accts = [_ra(1, 99, 10, active=True), (2, "b@x", False, "no credentials")]
    assert menubar.decide_auto_switch(accts, 95, frozenset()) == ("no_candidate_unverifiable", None)


def test_decide_reactive_exhausted_stays_no_candidate():
    accts = [_ra(1, 99, 10, active=True), _ra(2, 96, 50)]  # peer over limit, readable
    assert menubar.decide_auto_switch(accts, 95, frozenset()) == ("no_candidate", None)


def test_plan_silent_outcomes_are_noop():
    st, s = menubar.MenuBarState(), menubar.MenuBarSettings()
    assert menubar.plan_auto_switch(("no_candidate_unverifiable", None), st, s, 1e9) == ("noop", None)
    assert menubar.plan_auto_switch(("all_session_limited", None), st, s, 1e9) == ("noop", None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_menubar.py -k "reactive_hysteresis or reactive_unverifiable or reactive_exhausted" -v`
Expected: FAIL — `decide_auto_switch` takes no `blocked` arg / doesn't return `no_candidate_unverifiable`.

- [ ] **Step 3: Implement**

Replace the body of `decide_auto_switch` in `src/claude_swap/menubar.py` with:

```python
def decide_auto_switch(
    accounts: list[tuple[int, str, bool, dict | str | None]],
    threshold: float,
    blocked=frozenset(),
) -> tuple[str, int | None]:
    """Reactive auto-switch: switch when the active account hits the threshold.

    ``blocked`` is the hysteresis set (account-number strings at/over limit); a
    blocked candidate must clear ``threshold - AUTO_HYSTERESIS`` to be eligible
    again. Returns ``("switch", num)``, ``("none", None)``,
    ``("unknown_active", None)``, ``("no_candidate", None)`` (all peers exhausted),
    or ``("no_candidate_unverifiable", None)`` (a peer's usage was unreadable).
    Total — never raises.
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
    any_unverifiable = False
    for num, _email, is_active, usage in accounts:
        if is_active:
            continue
        worst = _worst_pct(usage)
        if worst is None:
            any_unverifiable = True
            continue
        limit = threshold - AUTO_HYSTERESIS if str(num) in blocked else threshold
        if worst >= limit:
            continue
        seven = _window_pct(usage, "seven_day")
        five = _window_pct(usage, "five_hour")
        candidates.append((worst, seven, five, num))
    if not candidates:
        return ("no_candidate_unverifiable", None) if any_unverifiable else ("no_candidate", None)
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    return ("switch", candidates[0][3])
```

(`plan_auto_switch` is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_menubar.py -q`
Expected: PASS — new tests plus every existing `decide_auto_switch`/`plan_auto_switch` test (they call with the old 2-arg form, which now uses `blocked=frozenset()` and is behavior-identical for their cases).

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/menubar.py tests/test_menubar.py
git commit -m "feat(menubar): hysteresis + unverifiable outcome in reactive decide

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `decide_consume_first` + strategy dispatch helpers

**Files:**
- Modify: `src/claude_swap/menubar.py`
- Test: `tests/test_menubar.py`

**Interfaces:**
- Consumes: `AUTO_HYSTERESIS`, `_window_pct`, `_worst_pct`, `_resets_at_ts`.
- Produces:
  - `decide_consume_first(accounts, threshold, blocked=frozenset()) -> tuple[str, int | None]`
  - `limiting_pct_by_account(accounts, strategy) -> dict[str, float | None]`
  - `evaluate_strategy(strategy, accounts, threshold, blocked) -> tuple[str, int | None]`

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_menubar.py
def _cf(num, pct5, pct7, reset7, active=False):
    return (num, f"a{num}@x.com", active,
            {"five_hour": {"pct": pct5},
             "seven_day": {"pct": pct7, "resets_at": reset7}})

_R_EARLY = "2026-06-24T07:00:00+00:00"
_R_MID = "2026-06-25T07:00:00+00:00"
_R_LATE = "2026-06-26T07:00:00+00:00"


def test_consume_first_picks_soonest_weekly_reset():
    # active #1 resets late; #2 resets early -> switch to #2 (consume it first).
    accts = [_cf(1, 10, 20, _R_LATE, active=True), _cf(2, 10, 20, _R_EARLY)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("switch", 2)


def test_consume_first_stays_when_active_is_optimal():
    accts = [_cf(1, 10, 20, _R_EARLY, active=True), _cf(2, 10, 20, _R_LATE)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("none", None)


def test_consume_first_tie_break_headroom_then_rotation():
    # equal reset -> more headroom (lower worst) wins; then rotation order.
    accts = [_cf(1, 99, 99, _R_LATE, active=True),
             _cf(2, 40, 30, _R_EARLY), _cf(3, 10, 80, _R_EARLY)]
    # #2 worst=40, #3 worst=80 -> #2
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("switch", 2)


def test_consume_first_all_session_limited_is_silent():
    # everyone 5h-saturated but weekly has room -> temporary, silent stay.
    accts = [_cf(1, 99, 10, _R_EARLY, active=True), _cf(2, 98, 20, _R_LATE)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("all_session_limited", None)


def test_consume_first_exhausted_notifies():
    accts = [_cf(1, 99, 99, _R_EARLY, active=True), _cf(2, 98, 99, _R_LATE)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("no_candidate", None)


def test_consume_first_unverifiable_is_silent():
    accts = [_cf(1, 99, 99, _R_EARLY, active=True), (2, "b@x", False, None)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("no_candidate_unverifiable", None)


def test_consume_first_unknown_active():
    accts = [(1, "a@x", True, "no credentials"), _cf(2, 10, 20, _R_EARLY)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("unknown_active", None)


def test_limiting_pct_by_account_per_strategy():
    accts = [_ra(1, 80, 50, active=True), (2, "b@x", False, None)]
    assert menubar.limiting_pct_by_account(accts, "reactive") == {"1": 80.0, "2": None}
    assert menubar.limiting_pct_by_account(accts, "consume-first") == {"1": 80.0, "2": None}


def test_evaluate_strategy_dispatch():
    accts = [_cf(1, 10, 20, _R_LATE, active=True), _cf(2, 10, 20, _R_EARLY)]
    assert menubar.evaluate_strategy("consume-first", accts, 95, frozenset()) == ("switch", 2)
    # reactive: active not over limit -> none
    assert menubar.evaluate_strategy("reactive", accts, 95, frozenset()) == ("none", None)
```

(Note: `_ra` is defined in Task 3's tests. `_cf`'s 5h values: in `test_limiting_pct_by_account_per_strategy`, `_ra(1, 80, 50)` → 5h 80, worst 80, so both strategies give `{"1": 80.0}`. Use that account shape.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_menubar.py -k "consume_first or limiting_pct or evaluate_strategy" -v`
Expected: FAIL — functions don't exist.

- [ ] **Step 3: Implement**

Add to `src/claude_swap/menubar.py` (after `decide_auto_switch`):

```python
def decide_consume_first(
    accounts: list[tuple[int, str, bool, dict | str | None]],
    threshold: float,
    blocked=frozenset(),
) -> tuple[str, int | None]:
    """Proactive 'consume the soonest-resetting account first' strategy.

    Eligible accounts have 5h not blocked (hysteresis) AND 7d below the threshold;
    the eligible account whose 7d window resets soonest (then most headroom, then
    rotation order) is optimal. Returns ``("switch", num)``, ``("none", None)``
    (already optimal), ``("unknown_active", None)``, ``("no_candidate", None)``
    (all weekly-exhausted -> notify), ``("no_candidate_unverifiable", None)``, or
    ``("all_session_limited", None)`` (weekly room but all 5h-blocked -> silent).
    Total — never raises.
    """
    active = next((a for a in accounts if a[2]), None)
    if active is None:
        return ("none", None)
    if _window_pct(active[3], "five_hour") is None or _window_pct(active[3], "seven_day") is None:
        return ("unknown_active", None)

    eligible: list[tuple[float, float, int, int]] = []
    any_unverifiable = False
    any_weekly_room = False
    for idx, (num, _email, is_active, usage) in enumerate(accounts):
        five = _window_pct(usage, "five_hour")
        seven = _window_pct(usage, "seven_day")
        if five is None or seven is None:
            if not is_active:
                any_unverifiable = True
            continue
        if seven < threshold:
            any_weekly_room = True
        limit5 = threshold - AUTO_HYSTERESIS if str(num) in blocked else threshold
        if five < limit5 and seven < threshold:
            eligible.append((_resets_at_ts(usage.get("seven_day")), _worst_pct(usage), idx, num))
    if not eligible:
        if any_unverifiable:
            return ("no_candidate_unverifiable", None)
        if any_weekly_room:
            return ("all_session_limited", None)
        return ("no_candidate", None)
    eligible.sort(key=lambda e: (e[0], e[1], e[2]))
    best_num = eligible[0][3]
    if best_num == active[0]:
        return ("none", None)
    return ("switch", best_num)


def limiting_pct_by_account(
    accounts: list[tuple[int, str, bool, dict | str | None]],
    strategy: str,
) -> dict[str, float | None]:
    """Per-account 'limiting %' feeding the hysteresis FSM, per strategy.

    reactive -> worst-of(5h, 7d); consume-first -> the 5h axis. None when unknown.
    """
    out: dict[str, float | None] = {}
    for num, _email, _is_active, usage in accounts:
        if strategy == "consume-first":
            out[str(num)] = _window_pct(usage, "five_hour")
        else:
            out[str(num)] = _worst_pct(usage)
    return out


def evaluate_strategy(
    strategy: str,
    accounts: list[tuple[int, str, bool, dict | str | None]],
    threshold: float,
    blocked,
) -> tuple[str, int | None]:
    """Dispatch to the active strategy's decision function (unknown -> reactive)."""
    if strategy == "consume-first":
        return decide_consume_first(accounts, threshold, blocked)
    return decide_auto_switch(accounts, threshold, blocked)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_menubar.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/menubar.py tests/test_menubar.py
git commit -m "feat(menubar): add consume-first strategy and dispatch helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: wire strategy + hysteresis into the app (rumps glue)

**Files:**
- Modify: `src/claude_swap/menubar.py` (the `MenuBarApp` in `run()`)

**Interfaces:**
- Consumes: `evaluate_strategy`, `limiting_pct_by_account`, `next_blocked`, `AUTO_HYSTERESIS`, `AUTO_STRATEGY_CHOICES`, the new settings/state fields.
- Produces: no new public functions — glue only.

GUI glue (no headless tests); verify statically + by the manual checklist. Locate blocks by content (line numbers drift).

- [ ] **Step 1: Add the strategy submenu**

In `_settings_menu`, add a strategy picker (place it right after the existing "Auto-switch accounts" toggle item is added, before the threshold submenu):

```python
            strategy_menu = rumps.MenuItem("Auto-switch strategy")
            st_labels = {"reactive": "Reactive (threshold)",
                         "consume-first": "Consume-first (soonest reset)"}
            for name in AUTO_STRATEGY_CHOICES:
                ch = rumps.MenuItem(st_labels[name], callback=self._make_strategy(name))
                ch.state = 1 if self.settings.auto_switch_strategy == name else 0
                strategy_menu.add(ch)
            menu.add(strategy_menu)
```

Add the callback alongside the other auto-switch callbacks (e.g. near `_make_threshold`):

```python
        def _make_strategy(self, name):
            def cb(_sender):
                self.settings.auto_switch_strategy = name
                self._last_auto_eval = 0.0  # re-evaluate promptly on change
                self._save_and_rebuild()
            return cb
```

- [ ] **Step 2: Maintain `blocked` and dispatch on strategy in `_maybe_auto_switch`**

Replace the decision lines in `_maybe_auto_switch` (currently
`decision = decide_auto_switch(self.snapshot["accounts"], self.settings.auto_switch_threshold)`
followed by `action, num = plan_auto_switch(...)`) with:

```python
            accounts = self.snapshot["accounts"]
            strategy = self.settings.auto_switch_strategy
            threshold = self.settings.auto_switch_threshold
            limiting = limiting_pct_by_account(accounts, strategy)
            self.state.blocked = sorted(
                next_blocked(limiting, threshold, AUTO_HYSTERESIS, frozenset(self.state.blocked))
            )
            self.state.save(state_path)
            decision = evaluate_strategy(strategy, accounts, threshold, frozenset(self.state.blocked))
            action, num = plan_auto_switch(decision, self.state, self.settings, now)
```

(The rest of `_maybe_auto_switch` — the `switch` / `notify_noswap` handling — is unchanged.)

- [ ] **Step 3: Fetch all accounts when consume-first needs to evaluate**

In `_auto_tick`, the freshness-refresh branch currently calls `self.refresh_async()`. Make that refresh full when the strategy needs all accounts ranked. Replace that single call:

```python
            if now - self._snapshot_at > cadence and not self._refreshing:
                self.refresh_async(full=(self.settings.auto_switch_strategy == "consume-first"))
                return
```

- [ ] **Step 4: Static + import checks**

```bash
uv run python -m py_compile src/claude_swap/menubar.py
uv run python -c "import claude_swap.menubar; print('import ok')"
uv run pytest tests/test_menubar.py -q
```
Expected: compile ok; `import ok`; all menubar tests pass.

- [ ] **Step 5: Self-review the glue**

Confirm: `import rumps` is still only inside `run()`; the strategy submenu builds with correct checkmarks; `_maybe_auto_switch` updates+persists `blocked` before deciding and dispatches via `evaluate_strategy`; consume-first triggers a full fetch; no other behavior dropped.

- [ ] **Step 6: Manual verification on macOS**

```bash
uv tool install --force --reinstall '.[menubar]'
launchctl kickstart -k gui/$(id -u)/com.claude-swap.menubar
```
Verify by hand:
- Settings → "Auto-switch strategy" shows Reactive / Consume-first with the current one checked; switching persists.
- With consume-first on, the active account tracks the soonest-resetting account; switches occur as resets re-order.
- An account hovering at the threshold no longer flip-flops (hysteresis).
- `~/.claude-swap-backup/menubar_state.json` shows a `blocked` array that updates.

- [ ] **Step 7: Commit**

```bash
git add src/claude_swap/menubar.py
git commit -m "feat(menubar): strategy dispatch, hysteresis, consume-first fetch

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: document the strategy in the README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document**

In `README.md`, in the menu-bar "Auto-switch" content, add:

```markdown
**Strategies.** *Settings → Auto-switch strategy*:

- **Reactive** (default) — stays put until the active account crosses the
  threshold, then switches to the account with the most headroom.
- **Consume-first** — proactively keeps you on the account whose **weekly window
  resets soonest** (use-it-or-lose-it), switching as resets re-order the queue.
  It polls all accounts each tick (needed to rank them).

A small hysteresis dead band prevents switching back and forth when an account
hovers at the threshold.
```

- [ ] **Step 2: Verify the suite**

Run: `uv run pytest -q`
Expected: PASS (whole suite green).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(menubar): document reactive vs consume-first strategies

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Notes

- `plan_auto_switch` needs no change: it notifies only on `no_candidate` and routes
  every other non-`switch` outcome (including the new `no_candidate_unverifiable`
  and `all_session_limited`) to `noop`.
- The `blocked` set is maintained over ALL accounts each tick (active included), so
  a just-vacated over-limit account stays ineligible (via hysteresis) until it
  recovers below `threshold - AUTO_HYSTERESIS`.
- consume-first reuses the single `auto_switch_threshold` for both the 5h block
  threshold and the 7d cap, and still respects the cooldown via `plan_auto_switch`.
