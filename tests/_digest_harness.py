"""The auto-switch engine harness and the fixed-seed digest fleets, importable
without pytest or `tests.test_autoswitch`, so a child interpreter can run them
against another revision's `claude_swap` (the base-identity digest cases).
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path
from unittest.mock import patch

from claude_swap.autoswitch import AutoSwitchEngine, TickOutcome
from claude_swap.models import Platform
from claude_swap.settings import AutoSwitchSettings
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageEntry


class FakeClock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _entry_for(value: dict | str | None, now: float) -> UsageEntry:
    """Synthesize the store entry a live fetch would have produced."""
    if isinstance(value, dict):
        return UsageEntry(last_good=value, fetched_at=now, age_s=0.0)
    if isinstance(value, str):
        return UsageEntry(sentinel=value)
    return UsageEntry()


class EngineHarness:
    """Seeded switcher + engine + captured events, on the Linux file backend."""

    def __init__(self, temp_home: Path, *, engine_cls=None, **settings_kwargs):
        self.temp_home = temp_home
        # Base-vs-head comparison (TestOutcomeDigestAgainstBase) drives the
        # SAME switcher/settings/clock through a different `AutoSwitchEngine`
        # class (a pre-fix source loaded separately): this is the one hook
        # that needs, so the harness itself stays a single implementation.
        self._engine_cls = engine_cls or AutoSwitchEngine
        # get_backup_root() (switcher.py) resolves via Path.home() on every
        # platform (both its XDG branch and its legacy
        # get_legacy_backup_root() fallback honour it, paths.py:101-108),
        # but on LINUX/WSL, $XDG_DATA_HOME takes precedence over Path.home()
        # when set (paths.py:102-106). Patching Path.home() alone is not a
        # superset of patching $XDG_DATA_HOME alone: a developer/CI with
        # XDG_DATA_HOME exported would have every EngineHarness's XDG branch
        # resolve to that ONE ambient value regardless of Path.home(),
        # aliasing all harnesses in the process onto one store. Keep BOTH
        # patches: neither mechanism dominates the other, and each covers
        # the platforms/environments where the other is silent.
        with (
            patch("pathlib.Path.home", return_value=self.temp_home),
            patch.dict(
                os.environ,
                {"XDG_DATA_HOME": str(self.temp_home / ".local" / "share")},
            ),
        ):
            self.switcher = ClaudeAccountSwitcher()
            self.switcher.platform = Platform.LINUX
            self.switcher._setup_directories()
            self.switcher._init_sequence_file()
        self.settings = AutoSwitchSettings(**settings_kwargs)
        self.events: list = []
        self.clock = FakeClock()
        # Keep the usage store on the same fake clock as the engine so
        # freshness/claims/poll scheduling are deterministic in tests.
        self.switcher._usage_store.clock = self.clock
        self.engine = self._make_engine()

    def _make_engine(self, **kwargs) -> AutoSwitchEngine:
        return self._engine_cls(
            self.switcher,
            self.settings,
            self.events.append,
            clock=self.clock,
            **kwargs,
        )

    def seed(self, num: int, email: str, *, expires_at: int | None = None) -> None:
        oauth_blob: dict = {
            "accessToken": f"sk-{num}",
            "refreshToken": f"rt-{num}",
        }
        if expires_at is not None:
            oauth_blob["expiresAt"] = expires_at
        self.switcher._write_account_credentials(
            str(num), email, json.dumps({"claudeAiOauth": oauth_blob})
        )
        self.switcher._write_account_config(
            str(num),
            email,
            json.dumps({
                "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"},
            }),
        )
        data = self.switcher._get_sequence_data()
        data["accounts"][str(num)] = {
            "email": email,
            "uuid": f"uuid-{num}",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        if num not in data["sequence"]:
            data["sequence"].append(num)
            data["sequence"].sort()
        if data["activeAccountNumber"] is None:
            data["activeAccountNumber"] = num
        self.switcher._write_json(self.switcher.sequence_file, data)

    def make_live(self, email: str, num: int) -> None:
        (self.temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "sk-live", "refreshToken": "rt-live"},
        }))
        (self.temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"},
        }))

    def tick_with_usage(self, usage: dict) -> TickOutcome:
        entries = {
            num: _entry_for(value, self.clock.now) for num, value in usage.items()
        }
        return self.tick_with_entries(entries)

    def tick_with_entries(self, entries: dict[str, UsageEntry]) -> TickOutcome:
        with patch.object(
            self.switcher, "usage_entries_by_account", return_value=entries
        ):
            return self.engine.tick()

    def active_number(self) -> int | None:
        return self.switcher._get_sequence_data()["activeAccountNumber"]

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def state(self) -> dict:
        path = self.switcher.backup_dir / "autoswitch_state.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text())


# Weekly-reset instants in ascending order (all valid ISO-8601, absolute).
# The 2024 dates are all far in the FUTURE relative to FakeClock's epoch
# (1_000_000.0 ≈ 1970-01-12); _R_PAST is before it.
_R_PAST = "1970-01-10T00:00:00Z"
_R_SOON = "2024-01-05T00:00:00Z"
_R_LATER = "2024-01-08T00:00:00Z"
_R_LATEST = "2024-01-10T00:00:00Z"


def _usage7(pct5: float, pct7: float, reset7: str | None = None) -> dict:
    """Usage with an explicit 7-day window (utilization + optional reset)."""
    seven: dict = {"pct": pct7}
    if reset7:
        seven["resets_at"] = reset7
    return {"five_hour": {"pct": pct5}, "seven_day": seven}


def fleet(rng: random.Random, now: float):
    resets = (_R_SOON, _R_LATER, _R_LATEST)
    active = _entry_for(
        _usage7(rng.uniform(80, 99), rng.uniform(0, 50), rng.choice(resets)),
        now,
    )
    entries = {"1": active}
    stale_nums = set()
    for num in ("2", "3"):
        last_good = _usage7(
            rng.uniform(0, 60), rng.uniform(0, 60), rng.choice(resets)
        )
        roll = rng.random()
        if roll < 0.35:
            stale_nums.add(num)
            entries[num] = UsageEntry(
                last_good=last_good,
                fetched_at=now - rng.uniform(200, 2000),
                age_s=rng.uniform(200, 2000),
                consecutive_failures=rng.randint(3, 12),
                last_error=rng.choice(["http-429", "timeout"]),
                backoff_until=now + rng.uniform(50, 500),
                trust_extended=True,
            )
        elif roll < 0.45:
            # Collector-struck while still FRESH: the `token_dead()` half
            # of the exclusion, which the freshness half alone admits.
            stale_nums.add(num)
            entries[num] = UsageEntry(
                last_good=last_good, fetched_at=now, age_s=0.0,
                auth_dead_strikes=2,
            )
        elif roll < 0.55:
            # m-1: FRESH but just failed once -- `fetched_at` moves only
            # on success, `backoffUntil`/`consecutiveFailures` only on
            # failure, the reviewer's own shape. Decision-trusted
            # (well inside `STALE_OK_S`) -- deliberately NOT added to
            # `stale_nums`, so the assertions below can tell an
            # authorized exclusion from an over-exclusion: this cell
            # must never be the one a diff traces to.
            entries[num] = UsageEntry(
                last_good=last_good,
                fetched_at=now - rng.uniform(5, 60),
                age_s=rng.uniform(5, 60),
                consecutive_failures=1,
                last_error="http-429",
                backoff_until=now + rng.uniform(500, 3600),
            )
        else:
            entries[num] = _entry_for(last_good, now)
    return entries, stale_nums


def run_fleets(
    engine_cls,
    tmp_path: Path,
    tag: str,
    fleets: list[dict],
    strategy: str,
    **settings_kwargs,
):
    # `EngineHarness.__init__` only patches `Path.home()` for its own
    # setup; every OTHER test in test_autoswitch.py relies on the `temp_home`
    # fixture holding that patch for the whole test. This one drives
    # its own directories, so it holds the same patch itself; without
    # it `seed`/`make_live`/`current_account_number()` read the REAL
    # `$HOME` and every tick reads unmanaged-active-account.
    results = []
    for i, entries in enumerate(fleets):
        home = tmp_path / f"{tag}{i}"
        (home / ".claude").mkdir(parents=True)
        with (
            patch("pathlib.Path.home", return_value=home),
            patch.dict(
                os.environ,
                {
                    "HOME": str(home),
                    "USERPROFILE": str(home),
                    "XDG_DATA_HOME": str(home / ".local" / "share"),
                },
            ),
        ):
            h = EngineHarness(
                home,
                engine_cls=engine_cls,
                strategy=strategy,
                **settings_kwargs,
            )
            h.seed(1, "a@example.com")
            h.seed(2, "b@example.com")
            h.seed(3, "c@example.com")
            h.make_live("a@example.com", 1)
            outcome = h.tick_with_entries(entries)
        events = tuple((e.kind, getattr(e, "reason", None)) for e in h.events)
        results.append((outcome.name, h.active_number(), events))
    return results


def run_jobs(engine_cls, root: Path, jobs: list[dict], seed: int, n_fleets: int):
    """`run_fleets` for each `{"strategy", "settings", "fleets"}` job: `n_fleets`
    fleets drawn from `seed`, or the job's raw `{account: usage}` fleets verbatim
    (`fleets` not None). One call answers every job a base child is asked."""
    now = 1_000_000.0  # the FakeClock epoch
    out = []
    for j, job in enumerate(jobs):
        if job["fleets"] is None:
            rng = random.Random(seed)
            fleets = [fleet(rng, now)[0] for _ in range(n_fleets)]
        else:
            fleets = [
                {num: _entry_for(v, now) for num, v in raw.items()}
                for raw in job["fleets"]
            ]
        out.append(
            run_fleets(
                engine_cls, root, f"j{j}f", fleets, job["strategy"],
                **job["settings"],
            )
        )
    return out


def _iso_at(epoch: float) -> str:
    """An absolute epoch as the ISO-Z string a window's ``resets_at`` carries."""
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _random_fleet(seed: int, now: float, n: int = 5) -> dict:
    rng = random.Random(seed)
    fleet = {}
    for i in range(1, n + 1):
        fleet[str(i)] = {
            "five_hour": {"pct": rng.uniform(0, 100)},
            "seven_day": {
                "pct": rng.uniform(0, 100),
                "resets_at": _iso_at(now + rng.uniform(3600, 30 * 86400)),
            },
            "scoped": [{"name": "Fable", "pct": rng.uniform(0, 100)}],
        }
    return fleet


def _run_trace(home, strategy: str, model: str, seed: int) -> list:
    """One fresh ``home`` per call — a shared root across combos leaves the
    prior combo's switched-to account and backups behind, aliasing the next
    one's decisions onto stale state (measured: reusing one ``temp_home``
    across the four combos below moved a digest that a fresh root does not).

    ``Path.home()`` stays patched for the whole call, not only construction:
    ``make_live()`` and ``tick()`` both resolve the active account through
    ``paths.*``, which reads ``Path.home()`` live — patched only around
    ``EngineHarness.__init__`` (as the conftest ``temp_home`` fixture does
    NOT do here) reads every tick's active account off the real ambient
    home instead, and every tick reads `no-active-account` (measured).
    """
    home.mkdir()
    (home / ".claude").mkdir()
    with (
        patch("pathlib.Path.home", return_value=home),
        patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
    ):
        h = EngineHarness(home, model=model, threshold=90.0, strategy=strategy)
        fleet = _random_fleet(seed, h.clock.now)
        for num in fleet:
            h.seed(int(num), f"acc{num}@example.com")
        h.make_live("acc1@example.com", 1)
        trace = []
        for _ in range(8):
            n0 = len(h.events)
            outcome = h.tick_with_usage(fleet)
            events = [(e.kind, getattr(e, "reason", None)) for e in h.events[n0:]]
            trace.append([outcome.name, h.active_number(), events])
            h.clock.advance(300.0)
    return trace
