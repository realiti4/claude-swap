# Usage Rate-Limit Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the menu bar from showing "usage unavailable" by making usage fetching rate-limit aware: detect HTTP 429, back off globally, keep showing last-known numbers, and poll lighter (active account often, backups every 5 min).

**Architecture:** Three layers. `oauth.fetch_usage_for_account` reports 429 as a distinct `"rate limited"` sentinel. `switcher._collect_usage` adds a global backoff window (skip the network while rate-limited), last-known-good retention (never overwrite a real number with a failure), and an `only=` parameter to fetch a subset. The menu-bar worker uses `only=` to fetch just the active account most of the time and all accounts every 5 minutes. The CLI path (`only=None`) is byte-for-byte unchanged in behavior.

**Tech Stack:** Python 3.12+, stdlib `urllib`/`json`/`time`, `pytest`.

## Global Constraints

- The CLI's all-accounts usage consumers (`--list`, `--switch --strategy`, `--status`) must keep working: `_collect_usage(accounts_info)` with no `only` argument fetches all accounts exactly as today.
- `src/claude_swap/menubar.py` must still import WITHOUT `rumps` (lazy `import rumps` inside `run()` only).
- Rate limiting is **per IP** → one 429 backs off ALL accounts (global, not per-account).
- Fixed `_DEFAULT_BACKOFF = 90` seconds. No `Retry-After` parsing. No new user setting.
- Sentinel string is exactly `"rate limited"` (joins the existing `"no credentials"` convention; `usage_summary` already passes unknown strings through).
- End every commit message with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

### Task 1: 429 → `"rate limited"` sentinel in `oauth.fetch_usage_for_account`

**Files:**
- Modify: `src/claude_swap/oauth.py`
- Test: `tests/test_oauth.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: module constant `RATE_LIMITED = "rate limited"`; `fetch_usage_for_account(...)` returns `RATE_LIMITED` on HTTP 429 (instead of `None`), on both the initial request and the post-refresh retry. All other failures still return `None`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_oauth.py
import urllib.error
from unittest.mock import patch
from claude_swap import oauth


def _creds():
    import json
    return json.dumps({"claudeAiOauth": {"accessToken": "sk-test", "refreshToken": "rt"}})


def _http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, None)


def test_fetch_usage_returns_rate_limited_on_429():
    with patch.object(oauth, "request_usage_data", side_effect=_http_error(429)):
        result = oauth.fetch_usage_for_account("1", "a@x.com", _creds(), is_active=True)
    assert result == oauth.RATE_LIMITED


def test_fetch_usage_returns_none_on_other_http_error():
    with patch.object(oauth, "request_usage_data", side_effect=_http_error(500)):
        result = oauth.fetch_usage_for_account("1", "a@x.com", _creds(), is_active=True)
    assert result is None


def test_fetch_usage_returns_none_on_timeout():
    with patch.object(oauth, "request_usage_data", side_effect=TimeoutError("slow")):
        result = oauth.fetch_usage_for_account("1", "a@x.com", _creds(), is_active=True)
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_oauth.py -k rate_limited -v`
Expected: FAIL — `test_fetch_usage_returns_rate_limited_on_429` gets `None`, and `AttributeError` for `oauth.RATE_LIMITED`.

- [ ] **Step 3: Implement the sentinel**

In `src/claude_swap/oauth.py`, add the constant near the top (after the imports / other module constants):

```python
RATE_LIMITED = "rate limited"
```

In `fetch_usage_for_account`, change the return type hint to `dict | str | None` and update the two HTTPError handlers. The first `except urllib.error.HTTPError as e:` block becomes (add the 429 short-circuit as the FIRST thing inside it):

```python
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _logger.debug("Usage fetch rate limited (429)")
            return RATE_LIMITED
        _logger.debug("Usage fetch failed: %r", e)
        if (
            e.code != 401
            or is_active
            or not oauth
            or not oauth.get("refreshToken")
        ):
            return None
```

And the post-refresh retry's `except` (currently `except Exception as retry_error:`) becomes two handlers so a 429 there is also reported:

```python
        try:
            data = request_usage_data(new_token)
            return build_usage_result(data)
        except urllib.error.HTTPError as retry_error:
            if retry_error.code == 429:
                return RATE_LIMITED
            _logger.debug("Usage fetch failed after refresh: %r", retry_error)
            return None
        except Exception as retry_error:
            _logger.debug("Usage fetch failed after refresh: %r", retry_error)
            return None
```

(The trailing `except Exception as e: return None` that wraps the whole try — covering `URLError`/timeout — is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_oauth.py -v`
Expected: PASS (new + existing oauth tests).

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/oauth.py tests/test_oauth.py
git commit -m "feat(usage): report HTTP 429 as a distinct 'rate limited' sentinel

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: backoff + last-known-good + `only=` in `switcher._collect_usage`

**Files:**
- Modify: `src/claude_swap/switcher.py`
- Test: `tests/test_switcher.py`

**Interfaces:**
- Consumes: `oauth.RATE_LIMITED` (Task 1); `read_cache`/`write_cache`/`MISSING` (already imported); `oauth.fetch_usage_for_account`.
- Produces:
  - constants `_DEFAULT_BACKOFF = 90`, `_FOREVER = float("inf")`
  - `_collect_usage(self, accounts_info, only: set[str] | None = None) -> list[dict | str | None]`
  - `_rate_limited_until(self) -> float`, `_set_rate_limited_until(self, ts: float) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_switcher.py
import time as _time
from claude_swap import oauth as _oauth


class TestCollectUsageBackoff:
    """Rate-limit backoff, last-known-good retention, and the only= subset."""

    def _setup(self, temp_home):
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _info(self):
        creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-x"}})
        return [
            (1, "a@x.com", "", "", True, creds),
            (2, "b@x.com", "", "", False, creds),
        ]

    def _patch_fetch(self, monkeypatch, responses, counter):
        def fake(num, email, creds, is_active, persist_credentials=None):
            counter.append(num)
            return responses[num]
        monkeypatch.setattr(_oauth, "fetch_usage_for_account", fake)

    def test_429_arms_backoff_and_skips_network_next_call(self, temp_home, monkeypatch):
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        calls = []
        # First call: account 1 returns a usage dict, account 2 is rate limited.
        self._patch_fetch(monkeypatch, {"1": {"five_hour": {"pct": 10.0}},
                                        "2": _oauth.RATE_LIMITED}, calls)
        first = s._collect_usage(self._info())
        assert first[0] == {"five_hour": {"pct": 10.0}}
        assert s._rate_limited_until() > _time.time()  # backoff armed
        # Second call within the window: NO network calls; last-known-good for #1.
        calls.clear()
        second = s._collect_usage(self._info())
        assert calls == []                       # network skipped
        assert second[0] == {"five_hour": {"pct": 10.0}}   # retained
        assert second[1] == "rate limited"       # never had a dict

    def test_last_known_good_retained_on_transient_failure(self, temp_home, monkeypatch):
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        calls = []
        self._patch_fetch(monkeypatch, {"1": {"five_hour": {"pct": 7.0}}, "2": None}, calls)
        s._collect_usage(self._info())           # seed cache for #1
        # Now #1 fails (None); its prior dict must be retained.
        self._patch_fetch(monkeypatch, {"1": None, "2": None}, calls)
        # bypass the 15s shortcut by clearing it is unnecessary; only=None re-fetches
        # because the 15s cache holds dicts -> shortcut returns them. Force a real
        # fetch by waiting out the TTL is slow; instead assert via only= path:
        out = s._collect_usage(self._info(), only={"1", "2"})
        assert out[0] == {"five_hour": {"pct": 7.0}}   # retained, not erased

    def test_only_limits_network_to_subset(self, temp_home, monkeypatch):
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        calls = []
        self._patch_fetch(monkeypatch, {"1": {"five_hour": {"pct": 1.0}},
                                        "2": {"five_hour": {"pct": 2.0}}}, calls)
        s._collect_usage(self._info(), only={"1"})
        assert calls == ["1"]                    # only account 1 hit the network

    def test_backoff_expired_resumes_fetching(self, temp_home, monkeypatch):
        s = self._setup(temp_home)
        monkeypatch.setattr(s, "_live_session_pids", lambda *a: [])
        s._set_rate_limited_until(_time.time() - 1)   # already expired
        calls = []
        self._patch_fetch(monkeypatch, {"1": {"five_hour": {"pct": 5.0}},
                                        "2": {"five_hour": {"pct": 6.0}}}, calls)
        out = s._collect_usage(self._info(), only={"1", "2"})
        assert sorted(calls) == ["1", "2"]       # fetched again
        assert out[0] == {"five_hour": {"pct": 5.0}}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_switcher.py -k CollectUsageBackoff -v`
Expected: FAIL — `_rate_limited_until` / `_set_rate_limited_until` don't exist; `only=` is an unexpected kwarg.

- [ ] **Step 3: Implement**

In `src/claude_swap/switcher.py`, ensure `import time` is present at the top (add it next to the other stdlib imports if missing). Add the constants next to `_USAGE_CACHE_TTL = 15`:

```python
_USAGE_CACHE_TTL = 15  # seconds
_DEFAULT_BACKOFF = 90  # seconds to skip usage fetching after a 429 (per-IP)
_FOREVER = float("inf")  # TTL that ignores cache age (for last-known-good reads)
```

Add the two rate-limit-state helpers to the `ClaudeAccountSwitcher` class (place them just above `_collect_usage`):

```python
    def _rate_limit_path(self) -> Path:
        return self.backup_dir / "cache" / "usage_ratelimit.json"

    def _rate_limited_until(self) -> float:
        data = read_cache(self._rate_limit_path(), _FOREVER, default=None)
        if isinstance(data, dict) and isinstance(data.get("until"), (int, float)):
            return float(data["until"])
        return 0.0

    def _set_rate_limited_until(self, ts: float) -> None:
        write_cache(self._rate_limit_path(), {"until": ts})
```

Replace the whole `_collect_usage` method body with:

```python
    def _collect_usage(
        self,
        accounts_info: list[tuple[int, str, str, str, bool, str]],
        only: set[str] | None = None,
    ) -> list[dict | str | None]:
        """Fetch usage per account (cache-first), with per-IP 429 backoff.

        Each entry is a usage dict, the string ``"no credentials"`` /
        ``"rate limited"``, or ``None`` when the API call failed. While a 429
        backoff window is active, the network is skipped entirely and the
        last-known-good cache is returned. ``only`` (a set of account-number
        strings) restricts which accounts hit the network this call; the rest
        come from cache. ``only=None`` fetches all (the CLI behavior).
        """
        usage_cache_path = self.backup_dir / "cache" / "usage.json"
        account_keys = [str(info[0]) for info in accounts_info]

        # Last-known-good cache, ignoring the freshness TTL (for retention/backoff).
        prior = read_cache(usage_cache_path, _FOREVER, default=None)
        if not isinstance(prior, dict):
            prior = {}

        # Global per-IP backoff: skip the network while rate limited.
        if time.time() < self._rate_limited_until():
            return [
                prior[k] if isinstance(prior.get(k), dict) else "rate limited"
                for k in account_keys
            ]

        def fetch(
            account_info: tuple[int, str, str, str, bool, str]
        ) -> dict | str | None:
            num, email, _, _, is_active, creds = account_info
            if not creds or not oauth.extract_access_token(creds):
                return "no credentials"

            def persist(acct_num: str, acct_email: str, new_creds: str) -> None:
                with FileLock(self.lock_file):
                    self._write_account_credentials(acct_num, acct_email, new_creds)

            has_live_session = bool(self._live_session_pids(str(num), email))
            return oauth.fetch_usage_for_account(
                str(num), email, creds,
                is_active=is_active or has_live_session,
                persist_credentials=persist,
            )

        # Fresh-cache shortcut (CLI path only): reuse if <15s old and same keys.
        if only is None:
            cached = read_cache(usage_cache_path, _USAGE_CACHE_TTL)
            if (cached is not MISSING and isinstance(cached, dict)
                    and set(cached.keys()) == set(account_keys)):
                return [cached.get(k) for k in account_keys]

        to_fetch = (
            accounts_info if only is None
            else [info for info in accounts_info if str(info[0]) in only]
        )
        fetched: dict[str, dict | str | None] = {}
        if to_fetch:
            with ThreadPoolExecutor() as executor:
                fetched = dict(zip(
                    (str(info[0]) for info in to_fetch),
                    executor.map(fetch, to_fetch),
                ))

        # Merge: a fresh usage dict wins; otherwise retain the prior good dict;
        # otherwise store the sentinel/None. Accounts not fetched this round take
        # their prior cached value.
        rate_limited = False
        result_map: dict[str, dict | str | None] = {}
        for k in account_keys:
            if k in fetched:
                val = fetched[k]
                if val == "rate limited":
                    rate_limited = True
                if isinstance(val, dict):
                    result_map[k] = val
                elif isinstance(prior.get(k), dict):
                    result_map[k] = prior[k]
                else:
                    result_map[k] = val
            else:
                result_map[k] = prior.get(k)

        write_cache(usage_cache_path, result_map)
        if rate_limited:
            self._set_rate_limited_until(time.time() + _DEFAULT_BACKOFF)
        return [result_map[k] for k in account_keys]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_switcher.py -k CollectUsageBackoff -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the broader switcher + cache suites (no regressions)**

Run: `uv run pytest tests/test_switcher.py tests/test_cache.py tests/test_cli.py -q`
Expected: PASS (existing `--list`/`status`/strategy tests still green — `only=None` preserves behavior).

- [ ] **Step 6: Commit**

```bash
git add src/claude_swap/switcher.py tests/test_switcher.py
git commit -m "feat(usage): global 429 backoff, last-known-good retention, only= subset

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: active-first polling in the menu bar

**Files:**
- Modify: `src/claude_swap/menubar.py`
- Test: `tests/test_menubar.py`

**Interfaces:**
- Consumes: `switcher._collect_usage(accounts_info, only=...)` (Task 2).
- Produces: `_snapshot(switcher, full: bool = True) -> dict` (now selects active-only vs all); module constant `_FULL_REFRESH_EVERY = 300`; the app worker's active-first / 5-min-full cadence.

`_snapshot`'s `full→only` translation is unit-tested; the worker cadence (rumps glue) is verified manually.

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_menubar.py
def test_snapshot_full_fetches_all(monkeypatch):
    seen = {}
    class _SW:
        _logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None)})()
        def _build_accounts_info(self):
            creds = ""
            return [(1, "a@x", "", "", True, creds), (2, "b@x", "", "", False, creds)]
        def _collect_usage(self, info, only=None):
            seen["only"] = only
            return [None, None]
    menubar._snapshot(_SW(), full=True)
    assert seen["only"] is None  # full -> all accounts


def test_snapshot_incremental_fetches_active_only():
    seen = {}
    class _SW:
        _logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None)})()
        def _build_accounts_info(self):
            return [(1, "a@x", "", "", False, ""), (2, "b@x", "", "", True, "")]
        def _collect_usage(self, info, only=None):
            seen["only"] = only
            return [None, None]
    menubar._snapshot(_SW(), full=False)
    assert seen["only"] == {"2"}  # incremental -> only the active account
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_menubar.py -k snapshot_full -v`
Expected: FAIL — `_snapshot()` takes no `full` argument (`TypeError`).

- [ ] **Step 3: Update `_snapshot`**

In `src/claude_swap/menubar.py`, change `_snapshot` to translate `full` into `only`:

```python
def _snapshot(switcher, full: bool = True) -> dict:
    """Fetch accounts + usage off the main thread. Returns a render snapshot.

    Shape: ``{"accounts": [(num, email, is_active, usage), ...],
    "active_email": str | None, "active_usage": dict | str | None}``.
    ``full=False`` fetches only the active account over the network (backups come
    from cache) to stay under the usage endpoint's per-IP rate limit; ``full=True``
    fetches all. Never raises — failures degrade to empty/unknown.
    """
    try:
        accounts_info = switcher._build_accounts_info()
        only = None
        if not full:
            active = next((str(info[0]) for info in accounts_info if info[4]), None)
            only = {active} if active else None
        usages = switcher._collect_usage(accounts_info, only=only)
    except Exception:
        switcher._logger.debug("menubar snapshot failed", exc_info=True)
        return {"accounts": [], "active_email": None, "active_usage": None}

    accounts = []
    active_email = None
    active_usage = None
    for (num, email, _org, _uuid, is_active, _creds), usage in zip(accounts_info, usages):
        accounts.append((num, email, is_active, usage))
        if is_active:
            active_email, active_usage = email, usage
    return {
        "accounts": accounts,
        "active_email": active_email,
        "active_usage": active_usage,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_menubar.py -k snapshot -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Wire the worker cadence (rumps glue — manual-verify)**

Add the module constant near the other menubar constants (e.g. after `AUTO_CHECK_CHOICES`):

```python
_FULL_REFRESH_EVERY = 300  # seconds between full (all-account) usage refreshes
```

In `MenuBarApp.__init__`, add `self._last_full_fetch = 0.0` next to the other bookkeeping fields (`self._snapshot_at = 0.0`, etc.), and change the initial fetch to a full one:

```python
            self.refresh_async(full=True)  # first fetch is a full one
```

Replace `refresh_async` and `_worker` with `full`-aware versions:

```python
        def refresh_async(self, full=False):
            if self._refreshing:
                return
            self._refreshing = True
            threading.Thread(target=self._worker, args=(full,), daemon=True).start()

        def _worker(self, full):
            try:
                now = time.time()
                if now - self._last_full_fetch >= _FULL_REFRESH_EVERY:
                    full = True
                snap = _snapshot(self.switcher, full=full)
                self.snapshot = snap
                self._snapshot_at = time.time()
                if full:
                    self._last_full_fetch = self._snapshot_at
                self._dirty = True
            finally:
                self._refreshing = False
```

Update the other `refresh_async()` call sites so switches and the manual refresh force a full fetch (the display timer and the auto-switch freshness kick stay incremental):
- `on_refresh_now`: `self.refresh_async(full=True)`
- in `_make_switch_to`'s callback (after a successful switch): `self.refresh_async(full=True)`
- in `_switch`'s callback (after a successful switch): `self.refresh_async(full=True)`
- in `_maybe_auto_switch` (after a successful auto-switch): `self.refresh_async(full=True)`
- Leave `on_refresh_tick` → `self.refresh_async()` and the `_auto_tick` freshness `self.refresh_async()` as incremental.

- [ ] **Step 6: Static + import checks**

```bash
uv run python -m py_compile src/claude_swap/menubar.py
uv run python -c "import claude_swap.menubar; print('import ok')"
uv run pytest tests/test_menubar.py -q
```
Expected: compile ok; `import ok`; menubar tests pass.

- [ ] **Step 7: Manual verification on macOS**

```bash
uv tool install --force --reinstall '.[menubar]'
launchctl kickstart -k gui/$(id -u)/com.claude-swap.menubar
```
Verify by hand over a few minutes:
- The menu shows real `5h/7d` numbers and the title shows them; they no longer flip to "usage unavailable" each minute.
- If a 429 occurs, accounts keep their last numbers (not "unavailable"); a never-fetched account shows "rate limited".
- `~/.claude-swap-backup/cache/usage_ratelimit.json` appears with an `until` after a 429, and numbers resume after it passes.

- [ ] **Step 8: Commit**

```bash
git add src/claude_swap/menubar.py tests/test_menubar.py
git commit -m "feat(menubar): poll active account often, all accounts every 5 min

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Notes

- The `_collect_usage` 15s fresh-cache shortcut is intentionally limited to the
  `only=None` (CLI) path. On the menu-bar incremental path (`only={active}`) we
  always want a fresh active-account fetch, and backups are served from `prior`.
- During a backoff window, `--list`/`--status` also skip the network and show
  last-known numbers — intended (it stops the CLI from re-tripping the limit too).
- `time.time()` is used directly (this is the app/CLI, not a workflow script).
