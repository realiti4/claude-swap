"""Tests for the auto-switch engine (autoswitch.py)."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import oauth, poll_policy
from claude_swap.autoswitch import (
    IDLE_HOLD_MAX_S,
    NO_RESET_FALLBACK_S,
    RECOVERY_HORIZON_S,
    SPENT_HEADROOM_PCT,
    AllExhaustedEvent,
    AutoSwitchEngine,
    ConfigWarningEvent,
    ErrorEvent,
    NoSwitchEvent,
    PollEvent,
    QuarantineEvent,
    SwitchEvent,
    TickOutcome,
    UnquarantineEvent,
    _recovery_is_useful,
    pct_label,
)
from claude_swap.json_output import USAGE_FOREIGN_CREDENTIAL, USAGE_TOKEN_EXPIRED
from claude_swap.usage_store import FetchRecord, UsageEntry
from claude_swap.models import Platform
from claude_swap.settings import AutoSwitchSettings
from claude_swap.switcher import ClaudeAccountSwitcher


class FakeClock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _iso_at(epoch: float) -> str:
    """An absolute epoch as the ISO-Z string a window's ``resets_at`` carries."""
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _usage(pct: float, resets_at: str | None = None) -> dict:
    window: dict = {"pct": pct}
    if resets_at:
        window["resets_at"] = resets_at
    return {"five_hour": window, "seven_day": {"pct": 0.0}}


def _entry_for(value: dict | str | None, now: float) -> UsageEntry:
    """Synthesize the store entry a live fetch would have produced."""
    if isinstance(value, dict):
        return UsageEntry(last_good=value, fetched_at=now, age_s=0.0)
    if isinstance(value, str):
        return UsageEntry(sentinel=value)
    return UsageEntry()


class EngineHarness:
    """Seeded switcher + engine + captured events, on the Linux file backend."""

    def __init__(self, temp_home: Path, **settings_kwargs):
        self.temp_home = temp_home
        # get_backup_root() (switcher.py) resolves via Path.home() on every
        # platform (both its XDG branch and its legacy
        # get_legacy_backup_root() fallback honour it, paths.py:101-108) —
        # but on LINUX/WSL, $XDG_DATA_HOME takes precedence over Path.home()
        # when set (paths.py:102-106). Patching Path.home() alone is not a
        # superset of patching $XDG_DATA_HOME alone: a developer/CI with
        # XDG_DATA_HOME exported would have every EngineHarness's XDG branch
        # resolve to that ONE ambient value regardless of Path.home(),
        # aliasing all harnesses in the process onto one store. Keep BOTH
        # patches — neither mechanism dominates the other, and each covers
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
        return AutoSwitchEngine(
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


@pytest.fixture
def harness(temp_home: Path) -> EngineHarness:
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


class TestEngineHarnessIsolation:
    """Pins the guarantee `EngineHarness.__init__`'s per-instance isolation
    exists for (see its docstring): two harnesses built against the SAME
    `temp_home` must not resolve to the same store.

    The prior form of this guard had no test that could kill it — reverting
    the scoping left the full suite green
    (`test_a_non_429_recorded_through_record_does_not_take_the_margin` no
    longer depends on it; that test's premise now stands on its own
    `record()` call). Without a test, a future cleanup could delete the
    isolation silently, and the next multi-harness test would alias two
    accounts' stores into one without any assertion noticing.

    An earlier fix scoped isolation on `$XDG_DATA_HOME`, which
    `get_backup_root()` only consults on LINUX/WSL (paths.py:101) — so
    the guard's OWN skipif, added because the mechanism is genuinely absent
    off Linux, also left the guard itself untested there (measured: skip
    forced + scoping reverted, full suite SURVIVED). The aliasing it
    guards against is real off Linux (direct probe: two harnesses on
    distinct subtrees collapsed to one store under the legacy
    ~/.claude-swap-backup path, and h1's seeded account silently became
    h2's). `Path.home()` is honoured by get_backup_root() on EVERY platform
    (both its XDG branch and its `get_legacy_backup_root()` fallback —
    paths.py:101-108), so scoping there instead makes the guard, and this
    test, load-bearing everywhere and lets the skipif be deleted.

    A "distinct subtree" harness (`EngineHarness(temp_home / "h1")`, the
    pattern this test and `decision_at()` in `TestAdaptiveScheduler` use)
    supports ONLY `seed()` and other `backup_dir`-scoped switcher calls.
    `Path.home()` is patched for `EngineHarness.__init__` only, not for the
    harness's lifetime, so after construction it reverts to whatever the
    ambient fixture set (the `temp_home` ROOT, not the subtree) — meaning
    `switcher.home` (cached at construction, `switcher.py:272`) disagrees
    with what any later `paths.*` call resolves. `make_live()` writes under
    `self.temp_home` (the subtree) and raises `FileNotFoundError` there
    (the subtree's `.claude/` is never created), and even if it didn't, the
    file would land somewhere production's own path resolution would not
    read. Do NOT call `make_live()` — or anything else that reads
    `paths.*` after construction — on a subtree harness; build it on
    `temp_home` directly instead (see `harness` fixture / `TestDecisionTable`).
    """

    @pytest.mark.parametrize(
        "platform",
        [Platform.LINUX, Platform.WSL, Platform.MACOS, Platform.WINDOWS],
    )
    def test_two_harnesses_on_one_temp_home_get_distinct_stores(
        self, temp_home, monkeypatch, platform
    ):
        # Two DIFFERENT subtrees of the same temp_home, matching the
        # existing multi-harness usage pattern (see decision_at() in
        # TestAdaptiveScheduler) — EngineHarness scopes isolation off the
        # exact temp_home argument it is given, not off a shared ambient
        # one, so distinct subtrees are what the guard promises to keep
        # separate. Parametrized over Platform.detect() (patched here, read
        # once inside ClaudeAccountSwitcher() before the harness pins
        # .platform = LINUX for the switcher's own runtime checks) so the
        # guard is pinned on macOS/Windows CI too, not only whichever OS
        # happens to run this suite.
        monkeypatch.setattr(Platform, "detect", staticmethod(lambda: platform))
        h1 = EngineHarness(temp_home / "h1")
        h2 = EngineHarness(temp_home / "h2")
        assert h1.switcher.backup_dir != h2.switcher.backup_dir, (
            f"both harnesses ({platform}) resolved to {h1.switcher.backup_dir} "
            "— EngineHarness.__init__'s isolation is not doing its job, so "
            "two harnesses alias each other's sequence.json/credentials/cache"
        )

        h1.seed(1, "a@example.com")
        h2.seed(1, "z@example.com")
        assert h1.switcher._get_sequence_data()["accounts"]["1"]["email"] == (
            "a@example.com"
        ), "h2's seed() bled into h1's store"
        assert h2.switcher._get_sequence_data()["accounts"]["1"]["email"] == (
            "z@example.com"
        ), "h1's seed() bled into h2's store"

    def test_two_harnesses_with_xdg_data_home_set_get_distinct_stores(
        self, temp_home, monkeypatch
    ):
        """The `Path.home()` patch is NOT a superset of the
        `$XDG_DATA_HOME` one. `get_backup_root()` gives `$XDG_DATA_HOME`
        precedence over `Path.home()` on Linux/WSL
        (paths.py:101-107) -- so a developer/CI with `XDG_DATA_HOME` exported
        still gets two harnesses colliding on ONE store, even though each
        harness's `Path.home()` differs. The prior test above cannot see this
        because the autouse `_isolate_real_home` fixture unconditionally
        `delenv`s `XDG_DATA_HOME`, so it must be re-set here, after that
        fixture has already run, to reproduce the defect at all.
        """
        monkeypatch.setattr(Platform, "detect", staticmethod(lambda: Platform.LINUX))
        shared_xdg = temp_home / "shared-xdg"
        monkeypatch.setenv("XDG_DATA_HOME", str(shared_xdg))
        h1 = EngineHarness(temp_home / "h1")
        h2 = EngineHarness(temp_home / "h2")
        assert h1.switcher.backup_dir != h2.switcher.backup_dir, (
            f"both harnesses collapsed onto {h1.switcher.backup_dir} with "
            "XDG_DATA_HOME set -- Path.home() scoping alone does not "
            "override XDG_DATA_HOME's precedence on Linux/WSL"
        )

        h1.seed(1, "a@example.com")
        h2.seed(1, "z@example.com")
        assert h1.switcher._get_sequence_data()["accounts"]["1"]["email"] == (
            "a@example.com"
        ), "h2's seed() bled into h1's store"
        assert h2.switcher._get_sequence_data()["accounts"]["1"]["email"] == (
            "z@example.com"
        ), "h1's seed() bled into h2's store"


class TestDecisionTable:
    def test_below_threshold_is_no_action(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(50), "2": _usage(10), "3": _usage(10),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert harness.active_number() == 1
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_over_threshold_switches_to_max_headroom(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(40), "3": _usage(20),
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"
        assert switch.to_ref == {"number": 3, "email": "c@example.com"}
        assert harness.state()["lastSwitchTo"] == "3"

    def test_no_active_account(self, temp_home):
        h = EngineHarness(temp_home)
        assert h.engine.tick() is TickOutcome.NO_ACTION
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == [
            "no-active-account"
        ]

    def test_hysteresis_margin_blocks_marginal_candidates(self, harness):
        # threshold 90, hysteresis 10 → a candidate must beat the active
        # account's utilization by >= 10 points; 95→86 is only 9 better.
        # Failing the margin is NOT exhaustion: no all-exhausted event, no
        # reset-sleep — the next tick must stay at normal cadence so the
        # at-limit escape isn't missed when the active account tops out.
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(86), "3": _usage(88),
        })
        assert outcome is TickOutcome.BLOCKED
        assert harness.active_number() == 1
        assert not any(isinstance(e, AllExhaustedEvent) for e in harness.events)
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["no-qualifying-candidate"]
        assert harness.engine._sleep_until_ts is None
        delay = harness.engine._next_delay(outcome)
        assert delay <= 1.1 * harness.settings.interval_seconds

    def test_issue_115_strictly_better_candidate_switches(self, harness):
        # Regression for #115: active bound by 5h (99%), candidate bound by
        # 7d (89%). The old absolute bar (<= 80% used) vetoed the candidate;
        # the relative gate takes it: 89 < 90 and 99 - 89 >= 10.
        outcome = harness.tick_with_usage({
            "1": {"five_hour": {"pct": 99.0}, "seven_day": {"pct": 24.0}},
            "2": {"five_hour": {"pct": 3.0}, "seven_day": {"pct": 89.0}},
            "3": {"five_hour": {"pct": 95.0}, "seven_day": {"pct": 10.0}},
        })
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"
        assert harness.active_number() == 2

    def test_proactive_never_lands_at_or_over_threshold(self, temp_home):
        # threshold 80, hysteresis 5: the candidate at 85% is five points
        # better than the active 90%, but it already sits over the threshold
        # and would re-trigger on the very next tick — blocked.
        h = EngineHarness(temp_home, threshold=80.0, hysteresis_pct=5.0)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        outcome = h.tick_with_usage({"1": _usage(90), "2": _usage(85)})
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["no-qualifying-candidate"]

    def test_stable_landing_does_not_switch_back(self, temp_home):
        # Cooldown disabled so only the gate itself prevents flapping: after
        # 99→89 the roles reverse, and the old account (99%) can never beat
        # the new active (89%) — the move is one-way.
        h = EngineHarness(temp_home, cooldown_seconds=0.0)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        usage = {
            "1": {"five_hour": {"pct": 99.0}, "seven_day": {"pct": 24.0}},
            "2": {"five_hour": {"pct": 3.0}, "seven_day": {"pct": 89.0}},
        }
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        h.clock.advance(60)
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert h.active_number() == 2
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_mixed_unknown_and_exhausted_is_not_all_exhausted(self, harness):
        # One candidate at its limit, the other unreadable this tick: usage
        # could recover any moment, so no long reset-sleep.
        outcome = harness.tick_with_usage({
            "1": _usage(95),
            "2": _usage(100, "2026-07-03T12:00:00Z"),
            "3": None,
        })
        assert outcome is TickOutcome.BLOCKED
        assert not any(isinstance(e, AllExhaustedEvent) for e in harness.events)
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["no-qualifying-candidate"]
        assert harness.engine._sleep_until_ts is None
        delay = harness.engine._next_delay(outcome)
        assert delay <= 1.1 * harness.settings.interval_seconds

    def test_stale_beyond_trust_blocks_all_exhausted(self, harness):
        # One candidate exhausted on trusted-stale data, the other's data aged
        # past every trust window (no failures, no plan — just overdue): the
        # unknown candidate could be viable, so no long reset-sleep.
        now = harness.clock.now
        reset = "2026-07-05T12:00:00Z"
        outcome = harness.tick_with_entries({
            "1": UsageEntry(last_good=_usage(95), fetched_at=now, age_s=0.0),
            "2": UsageEntry(
                last_good=_usage(100, reset), fetched_at=now - 400, age_s=400.0,
                consecutive_failures=1, trust_extended=True,
            ),
            "3": UsageEntry(last_good=_usage(10), fetched_at=now - 400, age_s=400.0),
        })
        assert outcome is TickOutcome.BLOCKED
        assert not any(isinstance(e, AllExhaustedEvent) for e in harness.events)
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["no-qualifying-candidate"]

    def test_trusted_stale_exhausted_set_still_fires_all_exhausted(self, harness):
        # Every candidate at its limit, known only through trusted-stale data
        # (in failure state) — that is still "known and exhausted".
        now = harness.clock.now
        reset = "2026-07-05T12:00:00Z"
        stale_exhausted = UsageEntry(
            last_good=_usage(100, reset), fetched_at=now - 400, age_s=400.0,
            consecutive_failures=1, trust_extended=True,
        )
        outcome = harness.tick_with_entries({
            "1": UsageEntry(last_good=_usage(95), fetched_at=now, age_s=0.0),
            "2": stale_exhausted,
            "3": stale_exhausted,
        })
        assert outcome is TickOutcome.BLOCKED
        exhausted = next(
            e for e in harness.events if isinstance(e, AllExhaustedEvent)
        )
        assert exhausted.earliest_reset_at == reset

    def test_cooldown_suppresses_proactive(self, harness):
        harness.engine._mutate_state(
            lambda s: s.update(lastSwitchAt=harness.clock() - 10)
        )
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(10), "3": _usage(10),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)] == [
            "cooldown"
        ]

    def test_at_limit_bypasses_cooldown(self, harness):
        harness.engine._mutate_state(
            lambda s: s.update(lastSwitchAt=harness.clock() - 10)
        )
        outcome = harness.tick_with_usage({
            "1": _usage(100), "2": _usage(10), "3": _usage(50),
        })
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"
        assert harness.active_number() == 2

    def test_cooldown_expires(self, harness):
        harness.engine._mutate_state(
            lambda s: s.update(lastSwitchAt=harness.clock())
        )
        harness.clock.advance(400)  # past the 300s default cooldown
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(10), "3": _usage(50),
        })
        assert outcome is TickOutcome.SWITCHED

    def test_unknown_active_usage_waits_then_fails_over(self, harness):
        usage = {"1": None, "2": _usage(10), "3": _usage(50)}
        assert harness.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert harness.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert harness.tick_with_usage(usage) is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "failover"
        assert harness.active_number() == 2

    def test_known_active_usage_resets_unhealthy_counter(self, harness):
        unknown = {"1": None, "2": _usage(10), "3": _usage(10)}
        healthy = {"1": _usage(50), "2": _usage(10), "3": _usage(10)}
        harness.tick_with_usage(unknown)
        harness.tick_with_usage(unknown)
        harness.tick_with_usage(healthy)  # resets the counter
        assert harness.tick_with_usage(unknown) is TickOutcome.NO_ACTION
        assert harness.active_number() == 1

    def test_all_candidates_unknown_is_no_comparison(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": None, "3": None,
        })
        assert outcome is TickOutcome.BLOCKED
        assert [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)] == [
            "no-comparison"
        ]

    def test_tie_resolves_to_earliest_slot(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(30), "3": _usage(30),
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2

    def test_candidate_not_better_than_active_is_skipped(self, harness):
        # Active 91% used (9 headroom); candidates worse or equal → exhausted.
        outcome = harness.tick_with_usage({
            "1": _usage(91), "2": _usage(95), "3": _usage(99),
        })
        assert outcome is TickOutcome.BLOCKED
        assert harness.active_number() == 1

    def test_at_limit_escapes_hysteresis_bar(self, harness):
        # Active hard at 100%; the only room anywhere is a candidate at 85%,
        # which the proactive hysteresis bar (<=80%) would reject. At-limit is
        # an escape: any account with real headroom beats a blocked one.
        outcome = harness.tick_with_usage({
            "1": _usage(100), "2": _usage(85), "3": _usage(97),
        })
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"
        assert harness.active_number() == 2

    def test_at_limit_never_targets_another_at_limit_account(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(100), "2": _usage(100), "3": _usage(100),
        })
        assert outcome is TickOutcome.BLOCKED
        assert harness.active_number() == 1

    def test_failover_ignores_hysteresis_bar(self, harness):
        # Active usage unreadable (auth likely dead); the only candidate with
        # room sits above the hysteresis bar — failover takes it anyway.
        usage = {"1": None, "2": _usage(85), "3": _usage(100)}
        harness.tick_with_usage(usage)
        harness.tick_with_usage(usage)
        outcome = harness.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "failover"
        assert harness.active_number() == 2

    def test_unmanaged_live_login_is_never_touched(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        # The user logged in with an account cswap doesn't manage.
        h.make_live("stranger@example.com", 9)
        live_before = (temp_home / ".claude" / ".credentials.json").read_text()
        outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})
        assert outcome is TickOutcome.NO_ACTION
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["unmanaged-active-account"]
        assert (temp_home / ".claude" / ".credentials.json").read_text() == live_before

    def test_all_exhausted_carries_earliest_reset(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(100, "2026-07-03T12:00:00Z"),
            "2": _usage(100, "2026-07-03T10:30:00Z"),
            "3": _usage(100, "2026-07-03T11:00:00Z"),
        })
        assert outcome is TickOutcome.BLOCKED
        event = next(e for e in harness.events if isinstance(e, AllExhaustedEvent))
        assert event.earliest_reset_at == "2026-07-03T10:30:00Z"
        assert harness.engine._sleep_until_ts is not None

    @pytest.mark.parametrize("offset", [-60.0, 0.0])
    def test_all_exhausted_ignores_non_future_reset(self, harness, offset):
        from datetime import datetime, timezone

        reset = (
            datetime.fromtimestamp(harness.clock.now + offset, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        outcome = harness.tick_with_usage({
            "1": _usage(100, reset),
            "2": _usage(100, reset),
            "3": _usage(100, reset),
        })
        assert outcome is TickOutcome.BLOCKED
        event = next(e for e in harness.events if isinstance(e, AllExhaustedEvent))
        assert event.earliest_reset_at is None
        assert harness.engine._sleep_until_ts is None
        assert harness.engine._next_delay(outcome) == NO_RESET_FALLBACK_S


class TestIdleHold:
    """Active token expired while Claude Code owns it → hold, don't fail over."""

    _HELD = {"1": USAGE_TOKEN_EXPIRED, "2": _usage(10), "3": _usage(20)}

    def test_token_expired_holds_instead_of_failover(self, harness):
        for _ in range(6):  # far past unhealthy_ticks (3)
            assert harness.tick_with_usage(self._HELD) is TickOutcome.NO_ACTION
            harness.clock.advance(60)
        assert harness.active_number() == 1
        assert not any(isinstance(e, SwitchEvent) for e in harness.events)
        reasons = {e.reason for e in harness.events if isinstance(e, NoSwitchEvent)}
        assert reasons == {"active-idle"}
        assert harness.engine._unhealthy_ticks == 0

    def test_idle_hold_slows_cadence(self, harness):
        outcome = harness.tick_with_usage(self._HELD)
        assert outcome is TickOutcome.NO_ACTION
        assert harness.engine._next_delay(outcome) >= NO_RESET_FALLBACK_S

    def test_idle_hold_cap_escalates_to_failover(self, harness):
        assert harness.tick_with_usage(self._HELD) is TickOutcome.NO_ACTION
        harness.clock.advance(IDLE_HOLD_MAX_S + 1)
        # Past the cap the sentinel counts as unhealthy again → failover after
        # unhealthy_ticks (3) consecutive ticks.
        assert harness.tick_with_usage(self._HELD) is TickOutcome.NO_ACTION
        assert harness.tick_with_usage(self._HELD) is TickOutcome.NO_ACTION
        assert harness.tick_with_usage(self._HELD) is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "failover"

    def test_recovery_resets_the_hold_clock(self, harness):
        healthy = {"1": _usage(50), "2": _usage(10), "3": _usage(20)}
        harness.tick_with_usage(self._HELD)
        harness.clock.advance(IDLE_HOLD_MAX_S - 60)
        harness.tick_with_usage(healthy)  # user came back; token refreshed
        harness.clock.advance(120)
        # New expiry long after: the hold clock restarted, so still held.
        assert harness.tick_with_usage(self._HELD) is TickOutcome.NO_ACTION
        assert harness.engine._unhealthy_ticks == 0
        assert harness.active_number() == 1

    def test_plain_fetch_failure_still_counts_unhealthy(self, harness):
        # A None (network failure / dead creds) is NOT the idle sentinel:
        # unhealthy counting and the hold clock reset both apply.
        harness.tick_with_usage(self._HELD)
        unknown = {"1": None, "2": _usage(10), "3": _usage(20)}
        assert harness.tick_with_usage(unknown) is TickOutcome.NO_ACTION
        assert harness.engine._unhealthy_ticks == 1
        assert harness.engine._idle_hold_since is None

    def test_foreign_credential_sentinel_fails_over_instead_of_holding(
        self, harness
    ):
        """The foreign sentinel (live credential proven to be another
        account's) must NOT idle-hold like TOKEN_EXPIRED: holding preserves
        the drift, while the failover switch stashes the foreign credential
        and restores the slot's backup — the switch IS the repair."""
        foreign = {
            "1": USAGE_FOREIGN_CREDENTIAL, "2": _usage(10), "3": _usage(20),
        }
        assert harness.tick_with_usage(foreign) is TickOutcome.NO_ACTION
        assert harness.tick_with_usage(foreign) is TickOutcome.NO_ACTION
        assert harness.tick_with_usage(foreign) is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "failover"
        assert harness.engine._idle_hold_since is None


class TestAdaptiveScheduler:
    """End-to-end through the real store: O(1) baseline, escalations,
    skip-to-reset, movement-based cadence."""

    @pytest.fixture(autouse=True)
    def _no_profile_probe(self):
        """Collect passes whose active credential drifted from the slot
        backup probe the profile oracle before resyncing — unpatched, a real
        HTTP call. "Probe failed" (resync skipped) is inert for scheduler
        behavior."""
        with patch(
            "claude_swap.oauth.fetch_oauth_profile", return_value=None
        ):
            yield

    def _harness(self, temp_home, monkeypatch, accounts=3, **settings_kwargs):
        monkeypatch.setattr("claude_swap.switcher._FETCH_STAGGER_S", 0)
        h = EngineHarness(temp_home, **settings_kwargs)
        emails = ["a@example.com", "b@example.com", "c@example.com"]
        for num in range(1, accounts + 1):
            h.seed(num, emails[num - 1])
        h.make_live("a@example.com", 1)
        monkeypatch.setattr(h.switcher, "_live_session_pids", lambda *a: [])
        return h

    @staticmethod
    def _counting_fetch(counts, usage_by_num, errors_by_num=None):
        def fake(num, email, creds, is_active=False, persist_credentials=None,
                 **kwargs):
            counts[num] = counts.get(num, 0) + 1
            error = (errors_by_num or {}).get(num)
            if error:
                return oauth.UsageOutcome(None, error=error)
            value = usage_by_num.get(num)
            return oauth.UsageOutcome(dict(value) if value else None)
        return fake

    def _tick(self, h, counts, usage_by_num, errors_by_num=None):
        with patch(
            "claude_swap.oauth.try_fetch_usage_for_account",
            side_effect=self._counting_fetch(counts, usage_by_num, errors_by_num),
        ):
            return h.engine.tick()

    def test_baseline_fetches_active_plus_one_candidate(self, temp_home, monkeypatch):
        h = self._harness(temp_home, monkeypatch)
        usage = {"1": _usage(50), "2": _usage(10), "3": _usage(20)}
        counts: dict[str, int] = {}
        # t0: active (never fetched) + the stalest candidate.
        self._tick(h, counts, usage)
        assert counts == {"1": 1, "2": 1}
        # t60: active planned MIN_INTERVAL_S out; the never-fetched candidate
        # is the due one.
        h.clock.advance(60)
        self._tick(h, counts, usage)
        assert counts == {"1": 1, "2": 1, "3": 1}
        # t120: nobody due — everyone served from the store.
        h.clock.advance(60)
        self._tick(h, counts, usage)
        assert counts == {"1": 1, "2": 1, "3": 1}
        # t180: the active account's plan comes due.
        h.clock.advance(60)
        self._tick(h, counts, usage)
        assert counts == {"1": 2, "2": 1, "3": 1}

    def test_near_threshold_escalates_to_full_refresh(self, temp_home, monkeypatch):
        # threshold 90, margin 15 → active at 80% is within the escalation band.
        h = self._harness(temp_home, monkeypatch)
        counts: dict[str, int] = {}
        outcome = self._tick(
            h, counts, {"1": _usage(80), "2": _usage(10), "3": _usage(20)}
        )
        assert outcome is TickOutcome.NO_ACTION  # still below the threshold
        assert counts == {"1": 1, "2": 1, "3": 1}  # but everyone got refreshed

    def test_active_unknown_escalates_before_failover(self, temp_home, monkeypatch):
        h = self._harness(temp_home, monkeypatch, unhealthy_ticks=1)
        counts: dict[str, int] = {}
        outcome = self._tick(
            h, counts,
            {"2": _usage(10), "3": _usage(50)},
            errors_by_num={"1": "timeout"},
        )
        # Candidate data was refreshed in the same tick the failover ran on.
        assert counts == {"1": 1, "2": 1, "3": 1}
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_active_cadence_floor_and_decay(self, temp_home, monkeypatch):
        # The active account polls at MIN_INTERVAL_S first; unmoved usage
        # decays the interval ×1.5 toward ACTIVE_MAX_INTERVAL_S.
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(10), "2": _usage(20)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)  # never-fetched → fetched
        assert counts["1"] == 1
        for _ in range(2):  # ages 60s and 120s — inside the 180s floor
            h.clock.advance(60)
            self._tick(h, counts, usage)
        assert counts["1"] == 1
        h.clock.advance(60)  # age 180s → due again
        self._tick(h, counts, usage)
        assert counts["1"] == 2
        # Unmoved → interval decayed to 270s: not due at +240, due at +300.
        h.clock.advance(240)
        self._tick(h, counts, usage)
        assert counts["1"] == 2
        h.clock.advance(60)
        self._tick(h, counts, usage)
        assert counts["1"] == 3

    def test_urgent_cadence_when_burning_near_the_band(self, temp_home, monkeypatch):
        # Active moving inside the escalation band → 60s urgent cadence, so
        # a threshold crossing is seen within a minute of the previous poll.
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(70), "2": _usage(10)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        usage["1"] = _usage(80)  # burning: +10 pts, now inside the band
        h.clock.advance(180)
        self._tick(h, counts, usage)  # movement + in band → urgent plan
        assert counts["1"] == 2
        usage["1"] = _usage(84)
        h.clock.advance(60)
        self._tick(h, counts, usage)  # urgent plan due after only 60s
        assert counts["1"] == 3

    def test_in_band_without_movement_keeps_the_floor(self, temp_home, monkeypatch):
        # In the escalation band but not burning: no urgency — the normal
        # 180s floor applies (escalation keeps candidates fresh; it must not
        # re-fetch a fresh, unmoving active every tick).
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(80), "2": _usage(10)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        for _ in range(2):
            h.clock.advance(60)
            self._tick(h, counts, usage)
        assert counts["1"] == 1  # not due inside the floor
        h.clock.advance(60)
        self._tick(h, counts, usage)
        assert counts["1"] == 2

    def test_urgent_band_follows_the_threshold(self, temp_home, monkeypatch):
        # The urgent band is distance-to-threshold, not absolute pct: with
        # threshold 50 (band edge 35), movement at 40% engages the urgent
        # cadence that the default threshold would ignore.
        h = self._harness(temp_home, monkeypatch, accounts=2, threshold=50)
        usage = {"1": _usage(30), "2": _usage(10)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        usage["1"] = _usage(40)
        h.clock.advance(180)
        self._tick(h, counts, usage)  # movement inside the 35..50 band
        assert counts["1"] == 2
        usage["1"] = _usage(44)
        h.clock.advance(60)
        self._tick(h, counts, usage)  # urgent plan due after only 60s
        assert counts["1"] == 3

    def test_stale_candidate_plan_never_gates_the_active(
        self, temp_home, monkeypatch
    ):
        # Role change outside a cswap switch (e.g. manual login): the active
        # slot can carry a plan written while it was an idle candidate, up to
        # 600s out. The ACTIVE_MAX_INTERVAL_S age cap overrides it.
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(50), "2": _usage(20)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        h.switcher._usage_store.set_poll_plan(
            {"1": (h.clock.now + 600.0, 600.0)}, {"1": ("a@example.com", "")}
        )
        h.clock.advance(240)  # inside the bogus plan, under the age cap
        self._tick(h, counts, usage)
        assert counts["1"] == 1
        h.clock.advance(120)  # age 360 ≥ ACTIVE_MAX_INTERVAL_S
        self._tick(h, counts, usage)
        assert counts["1"] == 2

    def test_exhausted_active_is_rechecked_before_its_reset(
        self, temp_home, monkeypatch
    ):
        from datetime import datetime, timezone

        h = self._harness(temp_home, monkeypatch, accounts=1)
        reset_ts = h.clock.now + 7200.0
        reset_iso = (
            datetime.fromtimestamp(reset_ts, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        usage = {"1": _usage(100, reset_iso)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        assert counts["1"] == 1
        for _ in range(3):
            h.clock.advance(400)
            self._tick(h, counts, usage)
        assert counts["1"] == 2

    def test_engine_repairs_legacy_reset_parked_active_plan(
        self, temp_home, monkeypatch
    ):
        from datetime import datetime, timezone

        h = self._harness(temp_home, monkeypatch, accounts=1)
        reset_ts = h.clock.now + 86_400.0
        reset_iso = (
            datetime.fromtimestamp(reset_ts, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        usage = {"1": _usage(100, reset_iso)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        h.switcher._usage_store.set_poll_plan(
            {"1": (reset_ts, 300.0)}, {"1": ("a@example.com", "")}
        )

        h.clock.advance(400)
        self._tick(h, counts, usage)
        assert counts["1"] == 2
        entry = h.switcher._usage_store.entries(
            {"1": ("a@example.com", "")}
        )["1"]
        assert entry.next_poll_at is not None
        assert entry.next_poll_at < reset_ts

    def test_band_jump_is_seen_at_most_one_poll_late(
        self, temp_home, monkeypatch
    ):
        # Active at 40% jumps into the band between polls: the jump is picked
        # up on the next planned poll, escalates the same tick, and the
        # movement flips the active onto the urgent cadence.
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(40), "2": _usage(20)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        usage["1"] = _usage(80)
        h.clock.advance(60)
        self._tick(h, counts, usage)  # plan-skipped: still believed at 40%
        assert counts["1"] == 1
        h.clock.advance(120)
        self._tick(h, counts, usage)  # planned poll sees 80% → escalate-all
        assert counts["1"] == 2
        assert counts["2"] == 1  # at the TTL edge: still served, not refetched
        h.clock.advance(60)
        self._tick(h, counts, usage)  # movement in band → urgent cadence
        assert counts["1"] == 3
        assert counts["2"] == 2  # now stale → the escalation refreshes it

    def test_active_in_backoff_keeps_trusted_headroom(self, temp_home, monkeypatch):
        # The active account's fetches are being refused (429 with a long
        # Retry-After). Its last-good data ages past STALE_OK_S, but the
        # staleness is deliberate: headroom stays known, so no unhealthy
        # ticks and no escalate-all burst while the server is rate limiting.
        h = self._harness(temp_home, monkeypatch)
        usage = {"1": _usage(50), "2": _usage(10), "3": _usage(20)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        h.clock.advance(60)
        self._tick(h, counts, usage)
        h.switcher._usage_store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=600.0)},
            {"1": ("a@example.com", "")},
        )
        h.clock.advance(400)  # active data now well past STALE_OK_S, in backoff
        counts.clear()
        outcome = self._tick(h, counts, usage)
        assert outcome is TickOutcome.NO_ACTION
        assert h.engine._unhealthy_ticks == 0
        assert "1" not in counts  # backoff respected
        assert sum(counts.values()) == 1  # baseline slot only, no escalate-all

    def test_a_non_429_ask_is_bounded_at_its_own_trust_ceiling(
        self, temp_home, monkeypatch
    ):
        """A non-429 ask passes through untouched below its own trust ceiling,
        and is bounded AT it above — never left to park a row past the point
        `entries()` already reads it unknown.

        This test used to assert the opposite (`other == ask` at every ask up
        to 20000, i.e. no bound at all — `test_a_non_429_ask_passes_through_
        uncapped`). That was wrong: `_classify_usage_error` (oauth.py) parses
        Retry-After for ANY HTTPError code, not just 429, and the usage
        endpoint sits behind Cloudflare, which routinely emits Retry-After on
        503s. A `503 Retry-After: 86400` parked a row 24h with no bound at
        all — reproduced end-to-end (PR #197).

        A LATER FIX bounded it at `RETRY_AFTER_FLOOR_CAP_S` (4500) —
        reasoning "one ceiling for how long any ask can park a row" — but
        that is the 429 arm's ceiling, not this one's: `entries()` reads a
        non-429 row unknown once `TRUST_MAX_AGE_S` (3600) elapses past the
        last success, so a non-429 ask between 3600 and 4500 parked the row
        past its own trust for up to 900s (a regression against
        upstream/main introduced by this PR). The bound is now
        `TRUST_MAX_AGE_S`, the ceiling this arm's trust actually uses.

        Asks below the ceiling are asserted with `==`, not `<=`, so a
        reintroduced blanket clamp to something shorter still fails this
        test.
        """
        from claude_swap.usage_store import TRUST_MAX_AGE_S, _failure_backoff_s

        for ask in (601.0, 3600.0):
            other = _failure_backoff_s(1, ask, rate_limited=False)
            assert other == ask, (
                f"non-429 ask={ask:.0f} backs off {other:.0f}s — an ask below "
                "the trust ceiling must pass through untouched"
            )

        for ask in (3601.0, 4500.0, 7200.0, 10_000.0, 20_000.0, 86_400.0, float("inf")):
            other = _failure_backoff_s(1, ask, rate_limited=False)
            assert other == TRUST_MAX_AGE_S, (
                f"non-429 ask={ask} backs off {other}s, not the "
                f"{TRUST_MAX_AGE_S:.0f}s trust ceiling — a non-429 Retry-After "
                "can park a row past its own trust again"
            )

        # `float("inf")` and an overflow literal parse to the same IEEE inf
        # via `_classify_usage_error`'s `float(raw.strip())` (oauth.py); both
        # must land on the ceiling, never inf, or the row is wedged forever
        # and the wedge survives a restart (json.dumps writes the
        # non-standard `Infinity` literal).
        assert _failure_backoff_s(1, float("1e400"), rate_limited=False) == (
            TRUST_MAX_AGE_S
        ), "a 1e400 ask (parses to inf) must be bounded, not left infinite"

        # The margin still does its job where it was measured, and the 429
        # arm's own bound (already at the cap) is unaffected by this change.
        assert _failure_backoff_s(1, 3600.0, rate_limited=True) == 4500.0, (
            "the hour-scale 429 margin was lost"
        )
        assert _failure_backoff_s(1, float("inf"), rate_limited=True) == 4500.0, (
            "the 429 arm's own inf handling regressed"
        )

    def test_shortening_a_429_wait_cannot_move_when_the_row_goes_unknown(
        self, temp_home
    ):
        """Un-pollable and unknown are independent axes, and this is why.

        A previous round clipped the 429 wait to `min(earliest reset, fetchedAt
        + ceiling) - now`, reading the row's remaining trust as a second
        deadline the wait had to respect. The reasoning was that a wait running
        past that instant leaves the row un-pollable AND unknown, which the
        unhealthy-tick counter converts into a failover.

        The blind window is real. Shortening the wait does not touch it.
        `entries()` decides trust from `lastGood`/`fetchedAt`, and `record()`
        writes both in the SUCCESS branch only — a 429 refreshes neither. So
        the instant the row goes unknown is fixed by the last SUCCESSFUL fetch,
        and no choice of backoff can move it by one second. A shorter wait only
        samples that same instant more often, at one request each.

        Asserted by driving two histories that differ ONLY in how long they
        waited, and reading `decision_value()` at fixed, cadence-independent
        instants either side of the window's own reset boundary — NOT by
        polling in a loop and reporting the first sample that observes the
        flip. A loop's report is only as precise as its own stride, so a
        stride that happens to divide the reset boundary (1800 % 1800 == 0)
        agrees with a finer one by coincidence, not because the mechanism was
        exercised: pick a stride of 2000 instead of 1800 and the same true
        mechanism reports a different (later, sampling-limited) instant,
        making the comparison look broken when nothing moved. Checking the
        same two fixed instants for every cadence removes the coincidence.
        """
        from datetime import datetime, timezone

        from claude_swap.usage_store import FetchRecord

        def _at(base, seconds):
            return (
                datetime.fromtimestamp(base + seconds, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )

        def decision_at(home, stride, checkpoint):
            """`decision_value() is None`, landing the clock EXACTLY on
            `checkpoint` (never overshooting it), having recorded a 429 every
            `stride` seconds up to that point — so every cadence is sampled
            at the identical absolute instant, not at "whenever the loop
            happens to next check"."""
            h = EngineHarness(home)
            h.seed(1, "a@example.com")
            store = h.switcher._usage_store
            ids = {"1": ("a@example.com", "")}
            t0 = h.clock.now
            store.record({"1": FetchRecord(usage=_usage(50, _at(t0, 1800)))}, ids)
            elapsed = 0.0
            while elapsed < checkpoint:
                store.record(
                    {"1": FetchRecord(error="http-429", retry_after_s=3600.0)}, ids
                )
                step = min(stride, checkpoint - elapsed)
                h.clock.advance(step)
                elapsed += step
            assert h.clock.now - t0 == checkpoint  # landed exactly, not past it
            return store.entries(ids)["1"].decision_value() is None

        # A 37s stride and an 1801s one (effectively one big wait) do not
        # share a divisor with 1800 or with each other, unlike the pre-fix
        # pairing (1800 vs 1800) — a cadence that moved the lapse instant
        # could no longer hide behind stride alignment.
        for checkpoint, expect_unknown in ((1799.0, False), (1801.0, True)):
            hammered = decision_at(temp_home / f"short{checkpoint}", 37.0, checkpoint)
            honored = decision_at(temp_home / f"long{checkpoint}", 1801.0, checkpoint)
            assert hammered == honored == expect_unknown, (
                f"at +{checkpoint:.0f}s: hammered saw unknown={hammered}, "
                f"honored saw unknown={honored}, expected {expect_unknown} — "
                "the backoff cadence moved a deadline that belongs to the "
                "last successful fetch"
            )

    def test_a_re_block_chain_spends_one_request_per_block(self, temp_home):
        """A chain of blocks costs one request each, however long it runs.

        This test used to assert `waited <= max(trust_left, floor)`, pinning a
        clip that shortened each wait to the row's remaining trust. That bound
        is satisfied by a wait of ZERO, and once the trust was spent the
        `max(..., computed)` floor supplied one — turning each further block
        into a burst of exponential-curve retries. What it called the
        anti-hammer floor doing its job was the ask being discarded.

        The budget is what a re-block chain actually threatens.
        `poll_policy` measured ~28-30 requests per trailing hour, per ACCOUNT,
        shared across every machine holding it. So the invariant is a request
        count, not an interval: each block costs exactly one poll, and four
        consecutive hour-long blocks cost four.
        """
        from claude_swap.usage_store import FetchRecord

        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        store = h.switcher._usage_store
        ids = {"1": ("a@example.com", "")}
        t0 = h.clock.now

        store.record({"1": FetchRecord(usage=_usage(50))}, ids)

        polls = 0
        for _ in range(4):
            # One block: poll, get 429, honor the wait it hands back.
            polls += 1
            store.record(
                {"1": FetchRecord(error="http-429", retry_after_s=3600.0)}, ids
            )
            h.clock.now = store.entries(ids)["1"].backoff_until or 0.0

        elapsed = h.clock.now - t0
        assert polls == 4, f"{polls} requests for 4 blocks"
        assert elapsed >= 4 * 3600.0, (
            f"4 blocks of 3600s each elapsed only {elapsed:.0f}s — a wait was "
            "cut short of the deadline the server actually gave"
        )

    def test_the_margin_is_not_traded_away_for_a_dead_scoped_window(self, temp_home):
        """A scoped window that already ended the trust does not shorten the wait.

        An earlier revision trimmed the ask back to the deadline here, on the
        reasoning that parking past a dead trust bought blindness for nothing.
        Measured, the trim never salvaged the trust — the row is unknown at
        release either way (see
        `test_a_429_wait_is_the_deadline_plus_the_margin`) — while landing on
        the deadline re-blocks 20 of 35 times for a fresh hour (re-measured
        2026-08-03; of 35, not 38 raw gaps — 3 are negative, not a uniform
        mechanism (per-gap detail in the RETRY_AFTER_MARGIN_S comment),
        excluded from both numerator and denominator).

        So the wait stays deadline + margin whatever the scoped window says.
        What the scoped window still decides is whether the row SERVES its
        last_good, which `entries(models=...)` answers.
        """
        from claude_swap.usage_store import FetchRecord

        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        st = h.switcher._usage_store
        t0 = h.clock.now
        ident = {"1": ("a@example.com", "")}
        st.record({"1": FetchRecord(usage={
            "five_hour": {"pct": 50.0, "resets_at": _iso_at(t0 + 14400)},
            "seven_day": {"pct": 10.0, "resets_at": _iso_at(t0 + 400000)},
            "scoped": [{"name": "Fable", "pct": 60.0,
                        "resets_at": _iso_at(t0 + 1800)}],
        })}, ident)
        st.record({"1": FetchRecord(error="http-429", retry_after_s=3600.0)}, ident)
        waited = st.entries(ident, models=("Fable",))["1"].backoff_until - t0
        assert waited == 4500.0, (
            f"waited {waited:.0f}s — the wait is the server's deadline plus "
            "the margin, and a dead scoped window does not buy it back"
        )

    def test_an_expired_trust_does_not_turn_one_block_into_a_request_storm(
        self, temp_home
    ):
        """Spent trust is not a licence to retry; the server's ask still governs.

        The sibling tests all assert `waited <= trust_left`, which is the
        direction that produced the defect: they are satisfied by a wait of
        ZERO. Once the clip drove the wait below `computed`, the
        `max(..., computed)` floor took over and returned the exponential
        curve — capped at `BACKOFF_CAP_S = 600` — so a live 3600s
        `Retry-After` became a 30s wait and the row re-polled through its own
        block. Measured on the pre-fix form, a genuine 3600s block with the
        5h window resetting 1800s in:

            req       t  Retry-After  trust_left    wait
              1       0         3600        1800    1800
              2    1800         1800           0      60
              3    1860         1740           0     120
              4    1980         1620           0     240
              5    2220         1380           0     480
              6    2700          900           0     600
              7    3300          300           0     600

        Seven requests inside one block, against a ~28-30/hour budget SHARED
        by every machine on the account, and the last retry lands at
        deadline+300s — inside the +2..887s band `RETRY_AFTER_MARGIN_S` exists
        to clear (re-measured 2026-08-03). Upstream spends two.

        Every retry from #2 on is also un-pollable AND unknown, the exact state
        the clip was added to prevent: `record()` writes `lastGood`/`fetchedAt`
        in the SUCCESS branch only, so a 429 refreshes nothing and the row
        stays unknown however often it is polled.

        Asserts the request count and the landing offset, not `waited <=
        trust_left` — a bound satisfied by retrying immediately cannot catch
        this.
        """
        from claude_swap.usage_store import RETRY_AFTER_MARGIN_S, FetchRecord

        block_s = 3600.0
        h = EngineHarness(temp_home, model="Fable")
        h.seed(1, "a@example.com")
        st = h.switcher._usage_store
        t0 = h.clock.now
        ident = {"1": ("a@example.com", "")}
        st.record({"1": FetchRecord(usage={
            "five_hour": {"pct": 50.0, "resets_at": _iso_at(t0 + 1800)},
            "seven_day": {"pct": 10.0, "resets_at": _iso_at(t0 + 400000)},
        })}, ident)

        requests = 0
        while h.clock.now - t0 < block_s and requests < 40:
            requests += 1
            # Retry-After counts down to a FIXED deadline: 40 of 41 measured
            # blocks opened at exactly 3600 (re-measured 2026-08-03) and every
            # machine in an episode reported the same one.
            remaining = block_s - (h.clock.now - t0)
            st.record(
                {"1": FetchRecord(error="http-429", retry_after_s=remaining)},
                ident,
            )
            h.clock.now = st.entries(ident, models=("Fable",))["1"].backoff_until

        landed = h.clock.now - t0
        assert requests <= 2, (
            f"{requests} requests inside one {block_s:.0f}s block — upstream "
            "spends 2, and the usage endpoint's ~28-30/hour budget is shared "
            "across every machine on this account"
        )
        assert landed >= block_s + RETRY_AFTER_MARGIN_S, (
            f"the last retry lands at deadline+{landed - block_s:.0f}s, inside "
            f"the +2..{RETRY_AFTER_MARGIN_S:.0f}s band where 20 of 35 measured "
            "lapses re-blocked for a fresh hour"
        )

        # SECOND KILLING ASSERTION — the PARK BOUND itself.
        #
        # This test's own scenario asks exactly `block_s` = 3600s, where the
        # margin arm's uncapped sum (3600 + 900 = 4500) coincidentally lands
        # exactly ON `RETRY_AFTER_FLOOR_CAP_S`, so the loop above passes
        # identically whether the PARK BOUND is applied or not — confirmed by
        # mutation (removing the PARK BOUND entirely still leaves this test
        # green; re-measured 2026-08-03: 9 tests in the full suite die
        # without it, INCLUDING this test's own second assertion below — so
        # "and this was not that test" no longer holds; the loop above is
        # still one of the 9 that stays green without the bound, which is
        # exactly why this second assertion earns its keep). An ask
        # genuinely past the cap (4000s: 4000 + 900 = 4900, uncapped) is
        # needed to tell the two apart.
        from claude_swap.usage_store import (
            RETRY_AFTER_FLOOR_CAP_S,
            _failure_backoff_s,
        )

        past_cap_wait = _failure_backoff_s(1, 4000.0, rate_limited=True)
        assert past_cap_wait == RETRY_AFTER_FLOOR_CAP_S, (
            f"a 4000s ask (uncapped sum 4900s) waited {past_cap_wait:.0f}s, "
            f"not the {RETRY_AFTER_FLOOR_CAP_S:.0f}s PARK BOUND — an ask "
            "genuinely past the cap can park a row unboundedly again, the "
            "same request-storm shape this test otherwise guards"
        )

    def test_the_trim_never_lands_inside_the_re_block_band(self, temp_home):
        """A 429 wait must clear the WHOLE measured re-block band, not just
        avoid landing inside a window sized by the very margin under test.

        RETRY_AFTER_MARGIN_S is 900 because 20 of 35 measured lapses
        re-blocked at +2s..+887s past their own deadline (re-measured
        2026-08-03; "of 35" not "of 38": 3 of the 38 raw gaps are negative
        — not a uniform mechanism (per-gap detail in the
        RETRY_AFTER_MARGIN_S comment) — excluded from both numerator and
        denominator), each earning a fresh hour. So `(deadline, deadline +
        900)` — the MEASURED band, a literal, independent of whatever
        `RETRY_AFTER_MARGIN_S` happens to be configured to — is the interval
        a 429 wait must clear.

        ROUND-7 FINDING: the previous form of this assertion compared
        `waited` (which the code under test computed AS `3600 +
        RETRY_AFTER_MARGIN_S`) against an upper bound of `3600.0 +
        RETRY_AFTER_MARGIN_S` — the SAME margin constant on both sides. So
        the upper edge of the band always equalled the wait itself, and
        `x < x` is false for any margin, including 0 and 450 — the assertion
        could not fail regardless of what the margin was set to. Mutation-
        confirmed: `RETRY_AFTER_MARGIN_S = 0.0` and `= 450.0` both still
        passed, and neutralising `oauth.relevant_windows` to always return
        `[]` (removing the scoped-window mechanism entirely) also still
        passed. Fixed here by comparing against `MEASURED_BAND_S`, a literal
        that does not consume `RETRY_AFTER_MARGIN_S`.

        The `for scoped in (3700, 4000, 4400)` loop that used to wrap this
        assertion is deleted: `_failure_backoff_s` takes no window argument,
        and the ONLY mechanism that ever made a scoped reset change the
        computed wait — a trust-based clip against a soon-resetting window
        — was removed in an earlier round (see `usage_store.py`'s "NO TRUST
        TRIM AGAINST THE SERVER'S DEADLINE"). All three scoped values
        therefore drove byte-identical `waited`; the loop exercised nothing
        that differed between iterations. A single scoped window is kept
        below (not swept) to confirm the record()->entries() round trip
        still produces the deadline+margin wait with a live scoped binding
        present, not to distinguish scoped values from each other.
        """
        from claude_swap.usage_store import FetchRecord

        MEASURED_BAND_S = 900.0  # the measured re-block band; NOT RETRY_AFTER_MARGIN_S

        h = EngineHarness(temp_home, model="Fable")
        h.seed(1, "a@example.com")
        st = h.switcher._usage_store
        t0 = h.clock.now
        ident = {"1": ("a@example.com", "")}
        st.record({"1": FetchRecord(usage={
            "five_hour": {"pct": 50.0, "resets_at": _iso_at(t0 + 7200)},
            "seven_day": {"pct": 10.0, "resets_at": _iso_at(t0 + 30 * 86400)},
            "scoped": [{"name": "Fable", "pct": 60.0,
                        "resets_at": _iso_at(t0 + 4000)}],
        })}, ident)
        st.record(
            {"1": FetchRecord(error="http-429", retry_after_s=3600.0)}, ident
        )
        waited = st.entries(ident, models=("Fable",))["1"].backoff_until - t0
        assert waited >= 3600.0 + MEASURED_BAND_S, (
            f"waited {waited:.0f}s, {waited - 3600:.0f}s past the deadline — "
            f"short of the measured {MEASURED_BAND_S:.0f}s re-block band"
        )

    def test_a_re_block_chain_does_not_shorten_its_own_waits(self, temp_home):
        """Every block waits deadline + margin, however deep into the chain.

        A previous round rewrote this to `max(min(4500, trust_left), floor)`,
        on the reasoning that the row's own trust shrinks as the chain runs and
        a wait past it buys no data. The shrinking is real; acting on it is
        what was wrong. The stored trust is not a second deadline the wait must
        respect — see
        `test_shortening_a_429_wait_cannot_move_when_the_row_goes_unknown` —
        and clipping to it only drops later waits onto (or short of) the
        server's deadline, which is the 21-of-36 re-block band this PR exists
        to clear (re-measured 2026-08-03).

        So the invariant is a constant again. The five-hour window here resets
        at +16000 and the ceiling would bind at +7200, both well inside the
        chain: a wait that honors neither is the point.
        """
        from claude_swap.usage_store import FetchRecord

        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        st = h.switcher._usage_store
        t0 = h.clock.now
        ident = {"1": ("a@example.com", "")}
        st.record({"1": FetchRecord(usage={
            "five_hour": {"pct": 50.0, "resets_at": _iso_at(t0 + 16000)},
            "seven_day": {"pct": 0.0},
        })}, ident)

        for block in range(4):
            st.record(
                {"1": FetchRecord(error="http-429", retry_after_s=3600.0)}, ident
            )
            waited = st.entries(ident)["1"].backoff_until - h.clock.now
            assert waited == 4500.0, (
                f"block {block}: waited {waited:.0f}s, not the server's "
                "3600s deadline plus the 900s margin — a later block traded "
                "the margin away for trust it cannot salvage"
            )
            h.clock.advance(waited)

    def test_a_non_429_recorded_through_record_does_not_take_the_margin(
        self, temp_home
    ):
        """The call-site wiring, not just the helper.

        Every other test of the `rate_limited` guard calls
        `_failure_backoff_s` directly with an explicit keyword. Mutation-checked:
        deleting `rate_limited=` from `record()` — so every 503/504 falls back
        to the `True` default and takes the 429-only margin again — left the
        whole suite green. `record()` is the only path production reaches, so
        the guard was untested where it runs.

        Drives a REAL success through `record()` first, so `last_good` and
        `fetched_at` actually exist and are decision-trusted before the 503.
        The defect this test previously carried recorded neither
        (`last_good=None, fetched_at=None`) and asserted a trust
        relationship that was never exercised — masked by `EngineHarness`
        instances sharing one store (see its docstring), which supplied a
        `lastGood` left behind by an earlier test in the same file even with
        the success record deleted. Fixed at the harness level; this test's
        premise assertion now genuinely depends on the record() call above
        it, not on cross-test contamination.

        The ask is chosen strictly between `TRUST_MAX_AGE_S` (3600) and
        `RETRY_AFTER_FLOOR_CAP_S` (4500). Above `TRUST_MAX_AGE_S`, the
        correct non-429 wiring clips the wait to `TRUST_MAX_AGE_S` (its own
        trust ceiling, so a non-429 park never outlasts it). The buggy
        wiring (defaulting to `rate_limited=True`) takes the 429-only margin
        instead: `min(ask + 900, RETRY_AFTER_FLOOR_CAP_S)`
        = 4500 for any ask at or above 3600. The two provably disagree (3600
        vs 4500) for any ask in this range. `_classify_usage_error` parses
        Retry-After for ANY HTTPError code, so a 503 carrying this Retry-After
        is the reachable shape.
        """
        from claude_swap.usage_store import (
            RETRY_AFTER_FLOOR_CAP_S,
            TRUST_MAX_AGE_S,
            FetchRecord,
        )

        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        st = h.switcher._usage_store
        ident = {"1": ("a@example.com", "")}

        st.record({"1": FetchRecord(usage=_usage(50))}, ident)
        h.clock.advance(120.0)  # ages last_good, still well inside STALE_OK_S
        premise = st.entries(ident)["1"]
        assert premise.decision_value() is not None, "premise: last_good trusted"
        t1 = h.clock.now

        ask = (TRUST_MAX_AGE_S + RETRY_AFTER_FLOOR_CAP_S) / 2  # strictly between
        st.record({"1": FetchRecord(error="http-503", retry_after_s=ask)}, ident)
        entry = st.entries(ident)["1"]
        waited = entry.backoff_until - t1
        assert waited == TRUST_MAX_AGE_S, (
            f"a non-429 backed off {waited:.0f}s, not its own trust ceiling "
            f"{TRUST_MAX_AGE_S:.0f}s — it took the 429-only margin at the "
            "record() call site"
        )

    def test_all_exhausted_escalation_preserves_wider_plan(
        self, temp_home, monkeypatch
    ):
        h = self._harness(temp_home, monkeypatch)
        usage = {num: _usage(100) for num in ("1", "2", "3")}
        counts: dict[str, int] = {}
        assert self._tick(h, counts, usage) is TickOutcome.BLOCKED
        assert counts == {"1": 1, "2": 1, "3": 1}

        # Simulate the wider plan learned after repeated 429s. The next
        # all-exhausted wake may refresh other stale rows, but escalation must
        # not defeat this token's congestion-control interval.
        h.switcher._usage_store.set_poll_plan(
            {"2": (h.clock.now + 1800.0, 1800.0)},
            {"2": ("b@example.com", "")},
        )
        h.clock.advance(NO_RESET_FALLBACK_S)
        assert self._tick(h, counts, usage) is TickOutcome.BLOCKED
        assert counts["2"] == 1

    def test_exhausted_candidate_keeps_a_bounded_poll_plan(
        self, temp_home, monkeypatch
    ):
        h = self._harness(temp_home, monkeypatch)
        reset_iso = "2026-07-05T12:00:00Z"
        usage = {"1": _usage(50), "2": _usage(100, reset_iso), "3": _usage(20)}
        counts: dict[str, int] = {}
        for _ in range(3):
            self._tick(h, counts, usage)
            h.clock.advance(60)
        assert counts["2"] == 1
        entry = h.switcher._usage_store.entries(
            {"2": ("b@example.com", "")}
        )["2"]
        assert entry.poll_interval_s == poll_policy.EXHAUSTED_INTERVAL_S
        assert entry.next_poll_at is not None
        assert entry.next_poll_at <= (
            entry.fetched_at
            + poll_policy.EXHAUSTED_INTERVAL_S * (1 + poll_policy.JITTER_FRAC)
        )

    def test_poll_never_scheduled_past_a_window_reset(self, temp_home, monkeypatch):
        from datetime import datetime, timezone

        from claude_swap.autoswitch import RESET_SLACK_S

        # The candidate's default interval is 300s, but its 5h window resets
        # in 90s — its stored 40% is obsolete at the rollover, so the next
        # poll must be clamped to reset + slack rather than waiting it out.
        h = self._harness(temp_home, monkeypatch, accounts=2)
        reset_ts = h.clock.now + 90.0
        reset_iso = (
            datetime.fromtimestamp(reset_ts, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        usage = {"1": _usage(50), "2": _usage(40, reset_iso)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        entry = h.switcher._usage_store.entries(
            {"2": ("b@example.com", "")}
        )["2"]
        assert entry.next_poll_at == pytest.approx(reset_ts + RESET_SLACK_S)
        # Learned cadence untouched by the clamp.
        assert entry.poll_interval_s == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S

    def test_movement_adapts_poll_interval(self, temp_home, monkeypatch):
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(50), "2": _usage(10)}
        counts: dict[str, int] = {}

        def interval() -> float | None:
            return h.switcher._usage_store.entries(
                {"2": ("b@example.com", "")}
            )["2"].poll_interval_s

        self._tick(h, counts, usage)          # first data point → base interval
        assert interval() == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S  # 300s
        h.clock.advance(180)
        self._tick(h, counts, usage)          # not due yet (300s interval)
        assert counts["2"] == 1
        h.clock.advance(120)
        self._tick(h, counts, usage)          # unmoved → backs off ×1.5
        assert counts["2"] == 2
        assert interval() == 450.0
        h.clock.advance(450)
        usage["2"] = _usage(20)               # moved 10 pts on another machine
        self._tick(h, counts, usage)
        assert counts["2"] == 3
        assert interval() == 225.0            # halved: polled closer while moving

    def test_idle_hold_skips_candidate_polling(self, temp_home, monkeypatch):
        h = self._harness(temp_home, monkeypatch)
        # Active token locally expired. The first tick now ATTEMPTS the
        # locked refresh (the fix's whole point); when it fails transiently
        # (network down), the row enters a failure backoff and subsequent
        # ticks surface the expired sentinel statically → idle-hold, with no
        # candidate slot spent.
        (h.temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live", "refreshToken": "rt-live",
                "expiresAt": 1000,
            },
        }))
        # The slot backup must be expired too — a non-expired backup would be
        # restored without any POST (no failure, no backoff, no hold).
        h.seed(1, "a@example.com", expires_at=1000)
        usage = {"2": _usage(10), "3": _usage(20)}
        counts: dict[str, int] = {}
        with patch(
            "claude_swap.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(None, "network"),
        ):
            assert self._tick(h, counts, usage) is TickOutcome.NO_ACTION
            h.clock.advance(10)  # still inside the 30s failure backoff
            counts.clear()
            # Backoff established → the next tick polls nothing at all: the
            # active row is gated, the sentinel surfaces statically, and no
            # candidate slot is spent.
            assert self._tick(h, counts, usage) is TickOutcome.NO_ACTION
        assert counts == {}
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons[-1] == "active-idle"

    def test_poll_event_carries_fetch_errors(self, temp_home, monkeypatch):
        h = self._harness(temp_home, monkeypatch, accounts=2, unhealthy_ticks=3)
        counts: dict[str, int] = {}
        self._tick(
            h, counts, {"2": _usage(10)}, errors_by_num={"1": "http-429"}
        )
        poll = next(e for e in h.events if isinstance(e, PollEvent))
        assert poll.fetch_errors.get("1") == "http-429"
        assert "http-429" in poll.human()
        assert poll.to_json()["fetchErrors"] == {"1": "http-429"}

    def test_quarantined_candidate_never_consumes_the_poll_slot(
        self, temp_home, monkeypatch
    ):
        h = self._harness(temp_home, monkeypatch)
        h.engine._quarantine("2", "b@example.com", "invalid_grant")
        usage = {"1": _usage(50), "2": _usage(10), "3": _usage(20)}
        counts: dict[str, int] = {}
        for _ in range(3):
            self._tick(h, counts, usage)
            h.clock.advance(60)
        # The alternate slot always went to account 3; 2 is dead weight.
        assert "2" not in counts
        assert counts["3"] >= 1

    def test_expired_active_enters_idle_hold_even_during_backoff(
        self, temp_home, monkeypatch
    ):
        """Finding-2 regression: the owned+expired sentinel must not be hidden
        by the active row's failure backoff (e.g. a Retry-After window), or
        the engine would count unhealthy ticks toward a spurious failover."""
        from claude_swap.usage_store import FetchRecord

        h = self._harness(temp_home, monkeypatch)
        # Active token locally expired while an owner is present.
        (h.temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live", "refreshToken": "rt-live",
                "expiresAt": 1000,
            },
        }))
        # Active row sits in a long failure backoff → the fetch path (and its
        # own expired short-circuit) is unreachable this tick.
        h.switcher._usage_store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=600.0)},
            {"1": ("a@example.com", "")},
        )
        counts: dict[str, int] = {}
        outcome = self._tick(h, counts, {"2": _usage(10), "3": _usage(20)})
        assert outcome is TickOutcome.NO_ACTION
        assert h.engine._unhealthy_ticks == 0
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["active-idle"]

    def test_consume_first_hold_never_escalates_below_threshold(
        self, temp_home, monkeypatch
    ):
        """Flat-traffic guard: a below-threshold consume-first tick that ends
        in a hold (no switch would fire) keeps the O(1) baseline — the
        phase-2 escalation is reserved for ticks that would actually switch.
        The fetch-set spy also catches an accidental all-candidates request
        that reserve() would have served from the store without HTTP."""
        h = self._harness(temp_home, monkeypatch, strategy="consume-first")
        # Active resets soonest -> every tick holds already-consuming-soonest.
        # five_hour 50 mirrors the baseline-cadence test's active plan.
        usage = {
            "1": _usage7(50, 20, _R_SOON),
            "2": _usage7(10, 10, _R_LATER),
            "3": _usage7(10, 10, _R_LATEST),
        }
        counts: dict[str, int] = {}
        fetch_sets: list[set] = []
        real_collect = h.switcher.usage_entries_by_account

        def spying_collect(*args, **kwargs):
            fetch_sets.append(set(kwargs.get("fetch") or ()))
            return real_collect(*args, **kwargs)

        with patch.object(
            h.switcher, "usage_entries_by_account", side_effect=spying_collect
        ):
            for _ in range(4):  # t0, t60, t120, t180
                outcome = self._tick(h, counts, usage)
                assert outcome is TickOutcome.NO_ACTION
                h.clock.advance(60)
        # (a) HTTP volume identical to the baseline cadence under `best`.
        assert counts == {"1": 2, "2": 1, "3": 1}
        # (b) no collection ever requested the all-candidates escalation set.
        assert {"1", "2", "3"} not in fetch_sets

    def test_consume_first_stale_target_holds_then_switches(
        self, temp_home, monkeypatch
    ):
        """Stale-after-escalation: when the phase-2 refetch cannot freshen the
        chosen target (Retry-After backoff), the freshness gate holds with
        stale-usage instead of switching on old data; once the backoff lapses
        a later tick freshens the target and the switch lands."""
        h = self._harness(temp_home, monkeypatch, strategy="consume-first")
        counts: dict[str, int] = {}
        # Populate the store while the active account resets soonest (holds).
        view_a = {
            "1": _usage7(50, 20, _R_SOON),
            "2": _usage7(10, 10, _R_LATER),
            "3": _usage7(10, 10, _R_LATEST),
        }
        self._tick(h, counts, view_a)          # t0: fetches 1, 2
        h.clock.advance(60)
        self._tick(h, counts, view_a)          # t60: fetches 3
        assert counts == {"1": 1, "2": 1, "3": 1}
        # #2 enters a Retry-After backoff; its stored entry ages past the
        # serve TTL (180s) while staying inside decision trust (300s).
        h.switcher._usage_store.record(
            {"2": FetchRecord(error="http-429", retry_after_s=600.0)},
            {"2": ("b@example.com", "")},
        )
        h.clock.advance(181)                   # t241
        h.events.clear()
        # The active refetch now reports the LATEST reset, so stored #2
        # (age 241: decision-trusted, no longer fresh) is the provisional
        # pick — but phase 2 cannot freshen it through the backoff.
        view_b = {
            "1": _usage7(50, 20, _R_LATEST),
            "2": _usage7(10, 10, _R_LATER),
            "3": _usage7(10, 10, _R_LATEST),
        }
        outcome = self._tick(h, counts, view_b)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert "stale-usage" in reasons
        assert counts["2"] == 1  # the backoff kept every refetch off #2
        # Backoff lapses -> a later tick freshens #2 and the switch lands.
        h.events.clear()
        h.clock.advance(700)
        outcome = self._tick(h, counts, view_b)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "consume-first"


class TestApiKeyAccounts:
    def _mark_api_key(self, harness, num: int) -> None:
        data = harness.switcher._get_sequence_data()
        data["accounts"][str(num)]["kind"] = "api_key"
        harness.switcher._write_json(harness.switcher.sequence_file, data)

    def test_api_key_candidate_excluded_by_default(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.make_live("a@example.com", 1)
        self._mark_api_key(h, 2)
        outcome = h.tick_with_usage({"1": _usage(95), "2": "api key"})
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1

    def test_api_key_is_last_resort_when_included(self, temp_home):
        h = EngineHarness(temp_home, include_api_key_accounts=True)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        self._mark_api_key(h, 2)
        # A qualifying OAuth candidate wins over the API key...
        outcome = h.tick_with_usage({
            "1": _usage(95), "2": "api key", "3": _usage(10),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_api_key_used_when_oauth_exhausted(self, temp_home):
        h = EngineHarness(temp_home, include_api_key_accounts=True)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        self._mark_api_key(h, 2)
        outcome = h.tick_with_usage({
            "1": _usage(100), "2": "api key", "3": _usage(100),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_active_api_key_idles_engine(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "key@token.local")
        h.seed(2, "b@example.com")
        h.make_live("key@token.local", 1)
        self._mark_api_key(h, 1)
        outcome = h.tick_with_usage({"1": "api key", "2": _usage(10)})
        assert outcome is TickOutcome.NO_ACTION
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == [
            "active-api-key"
        ]


class TestFreshening:
    def test_near_expiry_target_is_refreshed_and_persisted(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=int(h.clock() * 1000) + 60_000)
        h.make_live("a@example.com", 1)

        rotated = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-2-new",
                "refreshToken": "rt-2-new",
                "expiresAt": int(h.clock() * 1000) + 3_600_000,
            }
        })
        live_creds_path = temp_home / ".claude" / ".credentials.json"
        live_before = live_creds_path.read_text()
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(rotated, None),
        ) as mock_refresh:
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})

        assert outcome is TickOutcome.SWITCHED
        mock_refresh.assert_called_once()
        # Freshening itself never touched the active store (the switch did,
        # afterwards, via _perform_switch): the rotated token must have gone
        # through the backup, and now be live.
        assert "sk-2-new" in live_creds_path.read_text()
        assert live_creds_path.read_text() != live_before

    def test_fresh_target_is_not_refreshed(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=int(h.clock() * 1000) + 3_600_000)
        h.make_live("a@example.com", 1)
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials"
        ) as mock_refresh:
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})
        assert outcome is TickOutcome.SWITCHED
        mock_refresh.assert_not_called()

    def test_invalid_grant_quarantines_and_tries_next(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=1)  # long expired
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(None, "invalid_grant"),
        ):
            outcome = h.tick_with_usage({
                "1": _usage(95), "2": _usage(10), "3": _usage(20),
            })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3  # next candidate after 2 was quarantined
        q = next(e for e in h.events if isinstance(e, QuarantineEvent))
        assert (q.number, q.reason) == ("2", "invalid_grant")
        assert "2" in h.state()["quarantine"]

    def test_transient_failure_skips_without_quarantine(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=1)
        h.make_live("a@example.com", 1)
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(None, "transient"),
        ):
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})
        assert outcome is TickOutcome.ERROR
        assert h.active_number() == 1
        assert not h.state().get("quarantine")
        assert any(isinstance(e, ErrorEvent) for e in h.events)

    def test_live_session_target_is_skipped_even_with_fresh_token(self, temp_home):
        # Auto never activates an account that has a live `cswap run` session:
        # dual refresh-token ownership with nobody reading the warning.
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=int(h.clock() * 1000) + 3_600_000)
        h.make_live("a@example.com", 1)
        with patch.object(
            h.switcher, "live_session_pids_for", return_value=[4242]
        ), patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials"
        ) as mock_refresh:
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})
        assert outcome is TickOutcome.BLOCKED
        mock_refresh.assert_not_called()
        assert h.active_number() == 1

    def test_live_session_near_expiry_is_skipped(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=1)  # long expired
        h.make_live("a@example.com", 1)
        with patch.object(
            h.switcher, "live_session_pids_for", return_value=[4242]
        ), patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials"
        ) as mock_refresh:
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})
        assert outcome is TickOutcome.BLOCKED
        mock_refresh.assert_not_called()
        assert h.active_number() == 1


class TestQuarantineLifecycle:
    def test_quarantine_persists_across_engine_instances(self, harness):
        harness.engine._quarantine("2", "b@example.com", "invalid_grant")
        harness.events.clear()
        fresh_engine = harness._make_engine()
        usage = {"1": _usage(95), "2": _usage(0), "3": _usage(50)}
        with patch.object(
            harness.switcher,
            "usage_entries_by_account",
            return_value={
                num: _entry_for(value, harness.clock.now)
                for num, value in usage.items()
            },
        ):
            outcome = fresh_engine.tick()
        # 2 has the most headroom but is quarantined → 3 wins.
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_replaced_credentials_lift_quarantine(self, harness):
        harness.engine._quarantine("2", "b@example.com", "invalid_grant")
        # User re-logged in and re-captured the slot: new refresh token.
        harness.switcher._write_account_credentials(
            "2",
            "b@example.com",
            json.dumps({
                "claudeAiOauth": {"accessToken": "sk-2b", "refreshToken": "rt-2b"},
            }),
        )
        harness.events.clear()
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(0), "3": _usage(50),
        })
        assert any(isinstance(e, UnquarantineEvent) for e in harness.events)
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2
        assert "2" not in (harness.state().get("quarantine") or {})

    def test_state_lock_preserves_concurrent_writes(self, harness):
        # Simulate another engine writing between our read and our write: the
        # RMW under the state lock must preserve its quarantine entry.
        harness.engine._mutate_state(
            lambda s: s.setdefault("quarantine", {}).update(
                {"3": {"email": "c@example.com", "reason": "invalid_grant",
                       "at": "x", "refreshTokenFingerprint": None}}
            )
        )
        harness.engine._mutate_state(lambda s: s.update(lastSwitchAt=123.0))
        state = harness.state()
        assert state["lastSwitchAt"] == 123.0
        assert "3" in state["quarantine"]


class TestDryRunAndNoOp:
    def test_dry_run_mutates_nothing(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        h.engine = h._make_engine(dry_run=True)
        live_before = (temp_home / ".claude" / ".credentials.json").read_text()

        outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})

        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.dry_run is True
        assert h.active_number() == 1  # unchanged
        assert (temp_home / ".claude" / ".credentials.json").read_text() == live_before
        assert h.state() == {}  # no lastSwitchAt recorded

    def test_dry_run_never_freshens_or_quarantines(self, temp_home):
        # A near-expiry target would normally be refreshed (a real token
        # rotation) and a dead one quarantined (a state write). Dry-run must
        # stop at the decision: no network, no writes of any kind.
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=1)  # long expired
        h.make_live("a@example.com", 1)
        h.engine = h._make_engine(dry_run=True)
        backup_before = h.switcher.read_account_credentials("2", "b@example.com")

        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials"
        ) as mock_refresh:
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})

        assert outcome is TickOutcome.SWITCHED  # reported the would-switch
        mock_refresh.assert_not_called()
        assert h.switcher.read_account_credentials("2", "b@example.com") == backup_before
        assert h.state() == {}  # no quarantine, no lastSwitchAt

    def test_dry_run_does_not_release_quarantines(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        h.engine._quarantine("2", "b@example.com", "invalid_grant")
        # Replace the credential — a real tick would lift the quarantine.
        h.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {"accessToken": "n", "refreshToken": "n"}}),
        )
        h.events.clear()
        h.engine = h._make_engine(dry_run=True)
        state_before = h.state()

        outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})

        assert not any(isinstance(e, UnquarantineEvent) for e in h.events)
        assert h.state() == state_before  # state file untouched
        # And the still-recorded quarantine keeps 2 out of the dry-run plan.
        assert outcome is TickOutcome.BLOCKED

    def test_already_active_result_is_noop(self, harness):
        with patch.object(
            harness.switcher,
            "switch_to",
            return_value={"switched": False, "reason": "already-active"},
        ):
            outcome = harness.tick_with_usage({
                "1": _usage(95), "2": _usage(10), "3": _usage(50),
            })
        assert outcome is TickOutcome.NO_ACTION
        assert "lastSwitchAt" not in harness.state()


class TestEventsShape:
    def test_every_event_has_envelope(self, harness):
        harness.tick_with_usage({"1": _usage(95), "2": _usage(10), "3": _usage(50)})
        assert harness.events
        for event in harness.events:
            payload = event.to_json()
            assert payload["schemaVersion"] == 1
            assert payload["event"] == event.kind
            assert payload["ts"].endswith("Z")

    def test_switch_event_refs_match_account_ref_shape(self, harness):
        harness.tick_with_usage({"1": _usage(95), "2": _usage(10), "3": _usage(50)})
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        payload = switch.to_json()
        assert payload["from"] == {"number": 1, "email": "a@example.com"}
        assert payload["to"] == {"number": 2, "email": "b@example.com"}

    def test_poll_event_human_line(self, harness):
        harness.tick_with_usage({"1": _usage(42), "2": _usage(10), "3": None})
        poll = next(e for e in harness.events if isinstance(e, PollEvent))
        line = poll.human()
        assert "Account-1" in line and "42% used" in line
        # Others show per-window pcts, not just the ambiguous binding pct.
        assert "#2: 5h 10% · 7d 0%" in line
        assert "#3: ?" in line

    def test_poll_event_windows_match_the_decision_set(self, temp_home):
        # Scoped windows appear only when configured: rendering an ignored
        # Fable 100% next to a switch onto that account would read as a bug.
        usage = {
            "1": _usage(42),
            "2": {
                "five_hour": {"pct": 3.0},
                "seven_day": {"pct": 89.0},
                "scoped": [{"name": "Fable", "pct": 21.0}],
            },
        }

        def build(**kw):
            h = EngineHarness(temp_home, **kw)
            h.seed(1, "a@example.com")
            h.seed(2, "b@example.com")
            h.make_live("a@example.com", 1)
            return h

        plain = build()
        plain.tick_with_usage(usage)
        poll = next(e for e in plain.events if isinstance(e, PollEvent))
        assert "#2: 5h 3% · 7d 89%" in poll.human()
        assert "Fable" not in poll.human()
        assert poll.to_json()["windowsPct"]["2"] == {"5h": 3.0, "7d": 89.0}

        modeled = build(model="Fable")
        modeled.tick_with_usage(usage)
        poll = next(e for e in modeled.events if isinstance(e, PollEvent))
        assert "#2: 5h 3% · 7d 89% · Fable 21%" in poll.human()
        assert poll.to_json()["windowsPct"]["2"] == {
            "5h": 3.0, "7d": 89.0, "Fable": 21.0,
        }


class TestRunLoop:
    def test_loop_ticks_until_stopped(self, harness):
        ticks = []

        def fake_tick():
            ticks.append(1)
            if len(ticks) >= 2:
                harness.engine.stop()
            return TickOutcome.NO_ACTION

        with patch.object(harness.engine, "tick", side_effect=fake_tick), \
             patch.object(harness.engine._wake, "wait", return_value=None):
            assert harness.engine.run_loop() == 0
        assert len(ticks) == 2

    def test_loop_survives_raising_tick(self, harness):
        calls = []

        def raising_inner():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            harness.engine.stop()
            return TickOutcome.NO_ACTION

        with patch.object(
            harness.engine, "_tick_inner", side_effect=raising_inner
        ), patch.object(harness.engine._wake, "wait", return_value=None):
            harness.engine.run_loop()
        assert len(calls) == 2
        assert any(isinstance(e, ErrorEvent) for e in harness.events)

    def test_stop_before_start_is_not_lost(self, harness):
        # A stop() issued before the worker thread enters run_loop must not
        # be cleared away: the loop exits without a single tick.
        harness.engine.stop()
        with patch.object(harness.engine, "tick") as tick:
            assert harness.engine.run_loop() == 0
        tick.assert_not_called()

    def test_wake_during_tick_cuts_the_following_sleep_short(self, harness):
        # No wait patching on purpose: if the clear-at-top ordering were
        # wrong (wake cleared after the wait), the wake fired during tick 1
        # would be lost and the loop would block on the real 60s sleep —
        # caught by the join timeout instead of hanging the suite.
        ticks: list[int] = []

        def fake_tick():
            ticks.append(1)
            if len(ticks) == 1:
                harness.engine.wake()  # e.g. apply_threshold landed mid-tick
            else:
                harness.engine.stop()
            return TickOutcome.NO_ACTION

        with patch.object(harness.engine, "tick", side_effect=fake_tick):
            worker = threading.Thread(target=harness.engine.run_loop)
            worker.start()
            worker.join(timeout=10)
            finished = not worker.is_alive()
            harness.engine.stop()  # unblock a failing loop before asserting
            worker.join(timeout=5)
        assert finished
        assert len(ticks) == 2

    def test_blocked_with_reset_rechecks_at_exhausted_cadence(self, harness):
        harness.engine._sleep_until_ts = harness.clock() + 1800
        delay = harness.engine._next_delay(TickOutcome.BLOCKED)
        assert delay == poll_policy.EXHAUSTED_INTERVAL_S

    def test_blocked_exhausted_without_reset_uses_fallback(self, harness):
        harness.engine._sleep_until_ts = None
        harness.engine._blocked_wait_long = True
        assert harness.engine._next_delay(TickOutcome.BLOCKED) == 300.0

    def test_blocked_on_resolvable_condition_keeps_normal_cadence(self, harness):
        harness.engine._sleep_until_ts = None
        harness.engine._blocked_wait_long = False
        delay = harness.engine._next_delay(TickOutcome.BLOCKED)
        assert 0.9 * 60 <= delay <= 1.1 * 60

    def test_normal_delay_is_jittered_interval(self, harness):
        delay = harness.engine._next_delay(TickOutcome.NO_ACTION)
        assert 0.9 * 60 <= delay <= 1.1 * 60

    def test_sleep_cap(self, harness):
        harness.engine._sleep_until_ts = harness.clock() + 50 * 3600
        assert (
            harness.engine._next_delay(TickOutcome.BLOCKED)
            == poll_policy.EXHAUSTED_INTERVAL_S
        )


class TestLoopObeysThePollPlan:
    """The loop must not oversleep the plan the planner wrote.

    When the active account burns near the threshold the planner tightens its
    row to URGENT_INTERVAL_S so the crossing is caught quickly. The loop used
    to sleep ``interval_seconds`` regardless, so on any machine configured
    slower than the plan (360s here, the default) that plan could not be
    honoured: measured on the linux box mid-episode, the active row asked to
    be polled 112s ago while the engine still had minutes of sleep left, and
    the account sat over the threshold until the engine was restarted by hand.
    """

    def _plan(self, harness, *, due_in: float) -> None:
        num = harness.engine.switcher.current_account_number()
        real = harness.engine.switcher.usage_entries_by_account

        def patched(fetch=frozenset(), **kw):
            entries = dict(real(fetch=fetch, **kw))
            entries[num] = replace(
                entries[num], next_poll_at=harness.clock() + due_in
            )
            return entries

        harness.engine.switcher.usage_entries_by_account = patched

    def test_sleep_is_cut_to_the_rows_next_poll(self, harness):
        harness.engine.settings = replace(
            harness.engine.settings, interval_seconds=360.0
        )
        self._plan(harness, due_in=60.0)
        # Pre-fix this returned ~360s and the 60s plan silently ran late.
        assert harness.engine._next_delay(TickOutcome.NO_ACTION) == 60.0

    def test_never_sleeps_below_the_planners_own_floor(self, harness):
        """A row already overdue must not spin: the floor is the rate budget."""
        harness.engine.settings = replace(
            harness.engine.settings, interval_seconds=360.0
        )
        self._plan(harness, due_in=-500.0)
        assert (
            harness.engine._next_delay(TickOutcome.NO_ACTION)
            == poll_policy.URGENT_INTERVAL_S
        )

    def test_a_relaxed_plan_never_lengthens_the_sleep(self, harness):
        """Only ever shortens — a distant plan must not stretch the cadence
        past what the user configured."""
        harness.engine.settings = replace(
            harness.engine.settings, interval_seconds=60.0
        )
        self._plan(harness, due_in=3600.0)
        assert harness.engine._next_delay(TickOutcome.NO_ACTION) <= 1.1 * 60

    def test_a_store_failure_leaves_the_cadence_alone(self, harness):
        def boom(*a, **k):
            raise RuntimeError("store unreadable")

        harness.engine.switcher.usage_entries_by_account = boom
        delay = harness.engine._next_delay(TickOutcome.NO_ACTION)
        assert 0.9 * 60 <= delay <= 1.1 * 60


class TestSessionThreshold:
    """apply_threshold(): the TUI's session-only, mid-run override."""

    def test_apply_threshold_retargets_trigger_and_poll_pin(self, harness):
        harness.engine.apply_threshold(72.0)
        assert harness.engine.settings.threshold == 72.0
        # Poll-cadence planning follows the new value immediately.
        assert harness.switcher._poll_inputs_override == (72.0, ())
        # And the very next tick decides with it: 80% ≥ 72 switches, where
        # the constructed 90 would not have.
        outcome = harness.tick_with_usage({
            "1": _usage(80), "2": _usage(10), "3": _usage(10),
        })
        assert outcome is TickOutcome.SWITCHED

    def test_clear_poll_policy_inputs_unpins(self, harness):
        harness.engine.apply_threshold(72.0)
        harness.switcher.clear_poll_policy_inputs()
        assert harness.switcher._poll_inputs_override is None

    def _collect_fetch_sets(self, harness, threshold: float) -> list:
        entries = {
            n: _entry_for(_usage(80.0 if n == "1" else 10.0), harness.clock.now)
            for n in ("1", "2", "3")
        }
        with patch.object(
            harness.switcher, "usage_entries_by_account", return_value=entries
        ) as collect:
            harness.engine._collect_scheduled_usage("1", threshold=threshold)
        return [c.kwargs.get("fetch") for c in collect.call_args_list]

    def test_collect_escalates_on_the_tick_snapshot_threshold(self, harness):
        # Escalation must key on the threshold captured by the tick, not a
        # re-read of self.settings (engine settings stay at 90 throughout).
        # Active at 80%: within ESCALATION_MARGIN_PCT of 90 → full refresh...
        assert {"1", "2", "3"} in self._collect_fetch_sets(harness, 90.0)
        # ...but not of 99.9 → baseline fetching only.
        assert {"1", "2", "3"} not in self._collect_fetch_sets(harness, 99.9)


class TestPctLabel:
    def test_whole_numbers_drop_the_decimal(self):
        assert pct_label(90.0) == "90"

    def test_fractional_threshold_keeps_one_decimal(self):
        # .0f would render the valid maximum 99.9 as a lying "100".
        assert pct_label(99.9) == "99.9"

    def test_configured_precision_is_preserved(self):
        # settings.json accepts arbitrary floats; display must not round.
        assert pct_label(85.55) == "85.55"
        assert pct_label(85.555555) == "85.555555"

    def test_float_noise_is_absorbed(self):
        assert pct_label(100.0 - 37.4) == "62.6"
        assert pct_label(99.85000000000001) == "99.85"

    def test_poll_event_shows_fractional_threshold(self):
        poll = PollEvent(
            active={"number": 1, "email": "a@example.com"},
            headroom={"1": 40.0},
            threshold=99.9,
        )
        assert "switch at 99.9%" in poll.human()

    def test_below_threshold_detail_shows_fractional_threshold(self, temp_home):
        h = EngineHarness(temp_home, threshold=99.9)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        h.tick_with_usage({"1": _usage(50), "2": _usage(10)})
        details = [
            e.detail for e in h.events if isinstance(e, NoSwitchEvent)
        ]
        assert details == ["50% < 99.9%"]

    def test_below_threshold_detail_never_shows_impossible_comparison(
        self, temp_home
    ):
        # utilization 99.85 with threshold 99.9: .0f on the left side used
        # to render the logically impossible "100% < 99.9%".
        h = EngineHarness(temp_home, threshold=99.9)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        h.tick_with_usage({"1": _usage(99.85), "2": _usage(10)})
        details = [
            e.detail for e in h.events if isinstance(e, NoSwitchEvent)
        ]
        assert details == ["99.85% < 99.9%"]


class TestTokenIdentity:
    """The token endpoint's free identity data: uuid backfill and the
    identity-conflict detector (the zero-request check that catches a
    poisoned slot the moment auto freshens it)."""

    def test_uuid_backfill_from_token_account_on_freshen(self, harness):
        data = harness.switcher._get_sequence_data()
        data["accounts"]["2"]["uuid"] = ""
        harness.switcher._write_json(harness.switcher.sequence_file, data)
        # Slot 2 near expiry → freshen path runs.
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(
                fresh, None,
                {"uuid": "uuid-2-real", "email": "b@example.com",
                 "organizationUuid": ""},
            ),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "ok"
        assert harness.switcher._get_sequence_data()["accounts"]["2"]["uuid"] == (
            "uuid-2-real"
        )

    def test_conflicting_token_identity_returns_identity_conflict(self, harness):
        """A slot whose credential authenticates as a different account is not
        a viable target — but the rotated generation is still persisted (the
        grant consumed its predecessor)."""
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(
                fresh, None,
                {"uuid": "uuid-somebody-else", "email": "z@example.com",
                 "organizationUuid": ""},
            ),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "identity-conflict"
        # The consumed generation's successor was persisted regardless.
        assert harness.switcher.read_account_credentials(
            "2", "b@example.com"
        ) == fresh

    def test_identity_conflict_quarantines_instead_of_activating(self, harness):
        """Tick path: the conflicted slot is quarantined (wrong-account switch
        prevented); rotation falls through to the next candidate."""
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})

        def refresh(creds):
            data = json.loads(creds)["claudeAiOauth"]
            if data["refreshToken"] == "rt-2":
                return oauth.RefreshOutcome(
                    fresh, None,
                    {"uuid": "uuid-somebody-else", "email": "z@example.com",
                     "organizationUuid": ""},
                )
            return oauth.RefreshOutcome(creds, None)

        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            side_effect=refresh,
        ):
            outcome = harness.tick_with_usage({
                "1": _usage(95), "2": _usage(10), "3": _usage(80),
            })
        # Account 2 had the most headroom but is conflicted → quarantined,
        # and the switch landed elsewhere.
        assert "account-quarantined" in harness.kinds()
        q = harness.state().get("quarantine", {})
        assert q.get("2", {}).get("reason") == "identity-conflict"
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_dead_slot_quarantined_even_with_safety_copy_present(self, harness):
        """No automatic promotion (fail-open rework of the issue #117 guard):
        a dead slot is quarantined outright; safety copies are forensic
        material, and recovery is the documented /login + cswap add."""
        harness.switcher._store._write_unclaimed_credential(
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2-successor",
                "refreshToken": "rt-2-successor",
                "expiresAt": 99_999_999_999_000,
            }}),
            {"resolvedIdentity": {
                "uuid": "uuid-2", "email": "b@example.com",
                "organizationUuid": "",
            }},
        )
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2-dead", "refreshToken": "rt-2-dead",
                "expiresAt": 0,
            }}),
        )

        def refresh(creds):
            data = json.loads(creds)["claudeAiOauth"]
            if data["refreshToken"] == "rt-2-dead":
                return oauth.RefreshOutcome(None, "invalid_grant")
            return oauth.RefreshOutcome(creds, None)

        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            side_effect=refresh,
        ):
            outcome = harness.tick_with_usage({
                "1": _usage(95), "2": _usage(10), "3": _usage(80),
            })
        q = harness.state().get("quarantine", {})
        assert q.get("2", {}).get("reason") == "invalid_grant"
        # The safety copy was not consumed, and the switch landed elsewhere.
        assert len(harness.switcher.list_unclaimed_credentials()) == 1
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_same_uuid_different_org_is_identity_conflict(self, harness):
        """Organization is part of account identity everywhere else in the
        codebase: the same account uuid under a different org is a conflict
        (org compared only when both sides record one)."""
        data = harness.switcher._get_sequence_data()
        data["accounts"]["2"]["organizationUuid"] = "org-2"
        harness.switcher._write_json(harness.switcher.sequence_file, data)
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(
                fresh, None,
                {"uuid": "uuid-2", "email": "b@example.com",
                 "organizationUuid": "org-other"},
            ),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "identity-conflict"

    def test_malformed_token_identity_never_breaks_freshen(self, harness):
        """A schema change feeding a non-string uuid must be ignored, not
        raise — by this point the refreshed credential is already persisted,
        and a crash here would skip the persist bookkeeping and error the
        tick."""
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(
                fresh, None, {"uuid": 12345, "email": ["weird"]},
            ),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "ok"
        assert harness.switcher.read_account_credentials(
            "2", "b@example.com"
        ) == fresh

    def test_blank_uuid_slot_with_org_conflict_quarantines_not_backfills(
        self, harness,
    ):
        """Org conflict must be checked before the blank-uuid backfill: a
        wrong-org credential is evidence the slot holds the wrong account,
        and backfilling its uuid would stick a foreign identity onto the
        slot (backfill never rewrites a non-empty uuid). Blank-uuid slots
        with a recorded org are what accounts added by older versions look
        like."""
        data = harness.switcher._get_sequence_data()
        data["accounts"]["2"]["uuid"] = ""
        data["accounts"]["2"]["organizationUuid"] = "org-A"
        harness.switcher._write_json(harness.switcher.sequence_file, data)
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(
                fresh, None,
                {"uuid": "uuid-real", "email": "z@example.com",
                 "organizationUuid": "org-B"},
            ),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "identity-conflict"
        # The foreign uuid was NOT backfilled onto the slot.
        assert harness.switcher._get_sequence_data()["accounts"]["2"]["uuid"] == ""
        # The successor generation was still persisted (grant consumed it).
        assert harness.switcher.read_account_credentials(
            "2", "b@example.com"
        ) == fresh


def _model_usage(five_h: float, fable: float) -> dict:
    """Usage with a low 5h/7d but a per-model (Fable) weekly window."""
    return {
        "five_hour": {"pct": five_h},
        "seven_day": {"pct": 0.0},
        "scoped": [{"name": "Fable", "pct": fable}],
    }


class TestModelAwareSwitch:
    """`autoswitch.model` folds a per-model weekly limit into the decision."""

    def _seed(self, temp_home: Path, **kw) -> EngineHarness:
        h = EngineHarness(temp_home, **kw)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        return h

    def test_model_maxed_switches_despite_session_headroom(self, temp_home):
        # Active #1: 5h only 5% used, but Fable is maxed → must leave.
        h = self._seed(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _model_usage(5, 100),
            "2": _model_usage(5, 30),
            "3": _model_usage(5, 60),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2  # most Fable headroom
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.to_ref == {"number": 2, "email": "b@example.com"}

    def test_without_model_setting_the_same_usage_holds(self, temp_home):
        # Default engine ignores scoped windows → #1 reads 5% used, no switch.
        h = self._seed(temp_home)
        outcome = h.tick_with_usage({
            "1": _model_usage(5, 100),
            "2": _model_usage(5, 30),
            "3": _model_usage(5, 60),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_model_headroom_still_gated_by_session_window(self, temp_home):
        # Fable has room on every account, but #1's 5h is maxed → still leaves.
        h = self._seed(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _model_usage(100, 40),
            "2": _model_usage(10, 40),
            "3": _model_usage(20, 40),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2  # lowest binding (max of 5h, Fable)

    def test_comma_separated_models_switch_on_any(self, temp_home):
        # Configured for "Fable,Opus"; active #1 is fine on Fable but maxed on
        # Opus → must leave. Candidate scoped windows carry both models.
        h = self._seed(temp_home, model="Fable,Opus")

        def usage(five_h, fable, opus):
            return {
                "five_hour": {"pct": five_h},
                "seven_day": {"pct": 0.0},
                "scoped": [
                    {"name": "Fable", "pct": fable},
                    {"name": "Opus", "pct": opus},
                ],
            }

        outcome = h.tick_with_usage({
            "1": usage(5, 20, 100),   # Opus maxed
            "2": usage(5, 20, 30),    # most headroom
            "3": usage(5, 20, 70),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_all_sentinel_binds_every_scoped_window(self, temp_home):
        # "all" needs no names: each account's own scoped windows bind,
        # whatever they're called.
        h = self._seed(temp_home, model="all")
        outcome = h.tick_with_usage({
            "1": {"five_hour": {"pct": 5.0}, "seven_day": {"pct": 0.0},
                  "scoped": [{"name": "Sonnet", "pct": 100.0}]},
            "2": {"five_hour": {"pct": 5.0}, "seven_day": {"pct": 0.0},
                  "scoped": [{"name": "Sonnet", "pct": 20.0}]},
            "3": {"five_hour": {"pct": 5.0}, "seven_day": {"pct": 0.0},
                  "scoped": [{"name": "Opus", "pct": 60.0}]},
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_dual_exhausted_candidate_recovers_at_its_later_reset(self, temp_home):
        # #2 is blocked on both its 5h (resets 12:00) and Fable (15:00): it's
        # only usable again at the LATER one. #3 recovers later still (20:00),
        # so the all-exhausted wake is #2's Fable reset — which the old
        # earliest-of-any-window scan (12:00) would have jumped early for.
        h = self._seed(temp_home, model="Fable")
        fable_reset = "2026-07-05T15:00:00Z"
        outcome = h.tick_with_usage({
            "1": _model_usage(95, 10),
            "2": {
                "five_hour": {"pct": 100.0, "resets_at": "2026-07-05T12:00:00Z"},
                "seven_day": {"pct": 0.0},
                "scoped": [
                    {"name": "Fable", "pct": 100.0, "resets_at": fable_reset},
                ],
            },
            "3": {
                "five_hour": {"pct": 100.0, "resets_at": "2026-07-05T20:00:00Z"},
                "seven_day": {"pct": 0.0},
            },
        })
        assert outcome is TickOutcome.BLOCKED
        exhausted = next(e for e in h.events if isinstance(e, AllExhaustedEvent))
        assert exhausted.earliest_reset_at == fable_reset

    def test_unknown_recovery_falls_back_instead_of_oversleeping(self, temp_home):
        # #2 is exhausted with NO reset timestamp — it could recover any
        # moment. Sleeping toward #3's known 20:00 reset would suppress
        # checks for hours, so the wake time must be unprovable (bounded
        # blocked-cadence fallback instead of a reset sleep).
        h = self._seed(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _model_usage(95, 10),
            "2": {
                "five_hour": {"pct": 0.0},
                "seven_day": {"pct": 0.0},
                "scoped": [{"name": "Fable", "pct": 100.0}],  # no resets_at
            },
            "3": {
                "five_hour": {"pct": 100.0, "resets_at": "2026-07-05T20:00:00Z"},
                "seven_day": {"pct": 0.0},
            },
        })
        assert outcome is TickOutcome.BLOCKED
        exhausted = next(e for e in h.events if isinstance(e, AllExhaustedEvent))
        assert exhausted.earliest_reset_at is None
        assert h.engine._sleep_until_ts is None
        assert h.engine._next_delay(outcome) == NO_RESET_FALLBACK_S

    def test_scoped_only_exhaustion_drives_the_wake_time(self, temp_home):
        # Candidates blocked ONLY by Fable: the wake must come from the scoped
        # reset — the 5h/7d-only scan would find no ≥100% window at all.
        h = self._seed(temp_home, model="Fable")
        fable_reset = "2026-07-06T09:00:00Z"
        blocked = {
            "five_hour": {"pct": 3.0, "resets_at": "2026-07-05T12:00:00Z"},
            "seven_day": {"pct": 0.0},
            "scoped": [{"name": "Fable", "pct": 100.0, "resets_at": fable_reset}],
        }
        outcome = h.tick_with_usage({
            "1": _model_usage(95, 10), "2": blocked, "3": blocked,
        })
        assert outcome is TickOutcome.BLOCKED
        exhausted = next(e for e in h.events if isinstance(e, AllExhaustedEvent))
        assert exhausted.earliest_reset_at == fable_reset

    def test_scoped_binding_window_keeps_active_cadence_tight(self, temp_home):
        # Fable moving at 88% is inside the escalation band: with the model
        # configured the urgent cadence engages, while the 5%-used 5h window
        # alone would just decay the interval.
        kwargs = dict(
            prev_interval_s=poll_policy.MIN_INTERVAL_S,
            prev_usage=_model_usage(5, 84),
            new_usage=_model_usage(5, 88),
            is_active=True,
            threshold=90.0,
            recent_429=False,
            now=1000.0,
            rng=lambda: 0.5,
        )
        _, scoped = poll_policy.plan_after_fetch(models=("Fable",), **kwargs)
        assert scoped == poll_policy.URGENT_INTERVAL_S
        _, unscoped = poll_policy.plan_after_fetch(models=(), **kwargs)
        assert unscoped > poll_policy.MIN_INTERVAL_S  # plain decay

    def test_unmatched_model_name_warns_once(self, temp_home):
        h = self._seed(temp_home, model="Fabel")  # deliberate typo
        usage = {
            "1": _model_usage(5, 10),
            "2": _model_usage(5, 10),
            "3": _model_usage(5, 10),
        }
        h.tick_with_usage(usage)
        warnings = [e for e in h.events if isinstance(e, ConfigWarningEvent)]
        assert len(warnings) == 1
        assert "Fabel" in warnings[0].message
        assert warnings[0].to_json()["event"] == "config-warning"
        h.tick_with_usage(usage)
        warnings = [e for e in h.events if isinstance(e, ConfigWarningEvent)]
        assert len(warnings) == 1  # once per run, not per tick

    def test_no_false_warning_while_an_account_is_unreadable(self, temp_home):
        h = self._seed(temp_home, model="Fabel")
        h.tick_with_usage({
            "1": _model_usage(5, 10), "2": _model_usage(5, 10), "3": None,
        })
        assert not any(isinstance(e, ConfigWarningEvent) for e in h.events)
        # Once every account reports, the check completes and warns.
        h.tick_with_usage({
            "1": _model_usage(5, 10),
            "2": _model_usage(5, 10),
            "3": _model_usage(5, 10),
        })
        assert any(isinstance(e, ConfigWarningEvent) for e in h.events)

    def test_matching_name_never_warns(self, temp_home):
        h = self._seed(temp_home, model="Fable")
        h.tick_with_usage({
            "1": _model_usage(5, 10),
            "2": _model_usage(5, 10),
            "3": _model_usage(5, 10),
        })
        assert not any(isinstance(e, ConfigWarningEvent) for e in h.events)


# --- consume-first strategy ----------------------------------------------------

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


class TestConsumeFirstStrategy:
    def _harness(self, temp_home: Path) -> EngineHarness:
        h = EngineHarness(temp_home, strategy="consume-first")
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        return h

    def test_below_threshold_switches_to_soonest_weekly_reset(self, temp_home):
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),    # active resets later
            "2": _usage7(10, 10, _R_SOON),     # soonest -> consume first
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "consume-first"
        assert sw.to_ref == {"number": 2, "email": "b@example.com"}

    def test_stays_when_active_already_resets_soonest(self, temp_home):
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_SOON),     # active is soonest -> stay
            "2": _usage7(10, 10, _R_LATER),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["already-consuming-soonest"]

    def test_over_threshold_prefers_soonest_reset_over_max_headroom(self, temp_home):
        h = self._harness(temp_home)
        # Active over threshold -> must move. #2 has LESS headroom but resets
        # sooner; #3 has more headroom but resets latest. consume-first -> #2.
        outcome = h.tick_with_usage({
            "1": _usage7(95, 20, _R_LATER),
            "2": _usage7(50, 40, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_a_consume_first_target_must_still_be_healthy(self, temp_home):
        """The threshold landing gate has no cover on the consume-first path.

        `if (100.0 - h) >= settings.threshold and not all_above: continue` ->
        `if False` survives the whole suite. On the `best` path the hysteresis
        gate below masks it; consume-first has no headroom test at all — its
        `elif` compares weekly resets only, so with the gate gone a 96%-used
        account whose weekly window resets sooner is a valid target. Measured:

            active 1: 60 pts (util 40%), weekly reset 500h
            peer   2:  4 pts (util 96%), weekly reset  10h
            ORIGINAL ranking=[]      tick -> NO_ACTION
            MUTANT   ranking=['2']   tick -> SWITCHED to 2

        Landing there re-triggers on the very next tick, which is the harm the
        comment on that gate describes.

        TWO accounts on purpose: a third healthy peer would win the sort and
        hide the defect behind a correct answer.
        """
        h = EngineHarness(temp_home, strategy="consume-first")
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        outcome = h.tick_with_usage({
            "1": _usage7(40, 40, _R_LATEST),   # active, 60 pts, resets LAST
            "2": _usage7(96, 96, _R_SOON),     # 4 pts: sooner, but spent
        })
        assert outcome is not TickOutcome.SWITCHED, (
            "consume-first moved onto an account at 96% utilization because "
            "its weekly window resets sooner — it re-triggers next tick"
        )
        assert h.active_number() == 1

    def test_respects_cooldown(self, temp_home):
        h = self._harness(temp_home)  # default cooldown 300s
        h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert h.active_number() == 2  # switched to soonest
        h.events.clear()
        # Now a sooner account (#3) appears, but we're within cooldown.
        outcome = h.tick_with_usage({
            "2": _usage7(20, 20, _R_LATER),
            "1": _usage7(10, 10, _R_LATEST),
            "3": _usage7(10, 10, _R_SOON),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 2
        assert "cooldown" in [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]

    def test_locked_recheck_stops_concurrent_engine(self, temp_home):
        """The under-lock cooldown recheck in _perform must cover consume-first.

        The tick-level gate reads state *before* the lock, so an engine that
        read state before another engine's switch passes it on a stale
        snapshot; only the recheck inside _perform serializes the two. Drive a
        loser engine through _perform with a stale pre-lock read and a usage
        view that ranks a different target, and assert it backs off instead of
        double-switching inside the cooldown window.
        """
        h = self._harness(temp_home)  # default cooldown 300s
        loser = h._make_engine()
        # Winner: 1 -> 2 (soonest reset), records lastSwitchAt.
        h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert h.active_number() == 2
        h.events.clear()
        # Loser's first (pre-lock) state read predates the winner's write; its
        # usage view ranks #3 soonest, so it reaches _perform for a different
        # target and only the locked recheck can stop it.
        real_read = loser._read_state
        calls: list[bool] = []

        def racing_read() -> dict:
            calls.append(True)
            return {} if len(calls) == 1 else real_read()

        entries = {
            num: _entry_for(value, h.clock.now)
            for num, value in {
                "2": _usage7(20, 20, _R_LATER),
                "1": _usage7(10, 10, _R_LATEST),
                "3": _usage7(10, 10, _R_SOON),
            }.items()
        }
        with patch.object(loser, "_read_state", side_effect=racing_read):
            with patch.object(
                h.switcher, "usage_entries_by_account", return_value=entries
            ):
                outcome = loser.tick()
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 2  # no double-switch
        assert "cooldown" in [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]

    def test_reset_unknown_when_active_reset_missing(self, temp_home):
        # Active has no seven_day.resets_at: the strictly-sooner filter skips
        # every candidate, so the strategy is inert — say so, instead of the
        # false "already consuming soonest".
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20),              # no reset timestamp
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["reset-unknown"]

    def test_unreadable_candidates_stay_no_comparison(self, temp_home):
        # Every candidate unreadable this tick is a BLOCKED no-comparison for
        # any strategy — consume-first must not relabel it as a healthy hold.
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": None,
            "3": None,
        })
        assert outcome is TickOutcome.BLOCKED
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["no-comparison"]

    def test_exhausted_candidates_hold_without_false_reset_claim(self, temp_home):
        # All candidates at their limit while the active account is healthy:
        # staying put is right, but the detail must not claim the active
        # account resets first.
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(100, 100, _R_SOON),
            "3": _usage7(100, 100, _R_LATEST),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        holds = [e for e in h.events if isinstance(e, NoSwitchEvent)]
        assert [e.reason for e in holds] == ["already-consuming-soonest"]
        assert holds[0].detail == "no sooner-resetting account with room to spare"

    def test_single_account_below_threshold_is_no_action(self, temp_home):
        # Exit-code parity with `best`: a healthy below-threshold tick with
        # zero candidates is NO_ACTION/below-threshold, not BLOCKED/
        # no-candidates — cron wrappers key on the documented exit codes.
        h = EngineHarness(temp_home, strategy="consume-first")
        h.seed(1, "a@example.com")
        h.make_live("a@example.com", 1)
        outcome = h.tick_with_usage({"1": _usage7(20, 20, _R_SOON)})
        assert outcome is TickOutcome.NO_ACTION
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_api_key_only_peers_below_threshold_is_no_action(self, temp_home):
        # Same exit-code parity when the only alternatives are included
        # API-key accounts: they're never consume-first targets (no weekly
        # window), so a healthy below-threshold tick must stay
        # NO_ACTION/below-threshold — not fall through to a false
        # BLOCKED/no-comparison from the empty OAuth ranking.
        h = EngineHarness(
            temp_home, strategy="consume-first", include_api_key_accounts=True
        )
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.make_live("a@example.com", 1)
        data = h.switcher._get_sequence_data()
        data["accounts"]["2"]["kind"] = "api_key"
        h.switcher._write_json(h.switcher.sequence_file, data)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_SOON),
            "2": "api key",
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_skips_sooner_account_that_is_exhausted(self, temp_home):
        h = self._harness(temp_home)
        # #2 resets soonest but is itself at its limit (no headroom) -> ignored;
        # #3 resets later but has room and is sooner than active -> switch there.
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATEST),   # active resets latest
            "2": _usage7(100, 100, _R_SOON),   # soonest but exhausted
            "3": _usage7(10, 10, _R_LATER),    # sooner than active, has room
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_best_strategy_unaffected_below_threshold(self, temp_home):
        # Regression: default (best) still holds below threshold even when a
        # peer resets sooner — consume-first behavior must be opt-in.
        h = EngineHarness(temp_home)  # strategy defaults to "best"
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == [
            "below-threshold"
        ]

    def test_candidate_with_past_reset_is_not_selected(self, temp_home):
        # A stale snapshot whose resets_at has already elapsed means the
        # weekly window just rolled over — the LEAST perishable quota. It
        # must rank as unknown, never as "soonest".
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_PAST),     # inverted pick pre-fix
            "3": _usage7(10, 10, _R_SOON),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.to_ref == {"number": 3, "email": "c@example.com"}

    def test_active_past_reset_holds_reset_unknown(self, temp_home):
        # The active account's own reset can be stale too: past == unknown,
        # which lands on the existing reset-unknown hold.
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_PAST),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATER),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["reset-unknown"]

    def _two_phase_tick(
        self, h: EngineHarness, stored: dict, fresh: dict
    ) -> tuple[TickOutcome, list[set]]:
        """Drive one tick where stored-snapshot collections serve ``stored``
        and the all-candidates escalation serves ``fresh``.

        These ticks run outside the escalation band (utilization far below
        threshold - ESCALATION_MARGIN_PCT), so the collector never escalates
        on its own and the only all-candidates call a tick can make is the
        consume-first phase-2 refetch — the returned fetch sets prove whether
        it happened.
        """
        fetch_sets: list[set] = []

        def collect(fetch=None, **_kwargs):
            requested = set(fetch or ())
            fetch_sets.append(requested)
            view = fresh if requested == {"1", "2", "3"} else stored
            return {
                num: _entry_for(value, h.clock.now)
                for num, value in view.items()
            }

        with patch.object(
            h.switcher, "usage_entries_by_account", side_effect=collect
        ):
            outcome = h.engine.tick()
        return outcome, fetch_sets

    def test_two_phase_refetch_disqualifies_stale_pick(self, temp_home):
        # The stored snapshot ranks #2; the phase-2 refetch shows it
        # exhausted. The tick must re-decide on the fresh data and hold.
        h = self._harness(temp_home)
        stored = {
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        }
        fresh = {
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(100, 100, _R_SOON),   # burned out since the snapshot
            "3": _usage7(10, 10, _R_LATEST),
        }
        outcome, fetch_sets = self._two_phase_tick(h, stored, fresh)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["already-consuming-soonest"]
        assert fetch_sets.count({"1", "2", "3"}) == 1  # phase 2 fired once

    def test_two_phase_refetch_confirms_switch(self, temp_home):
        # Fresh data agrees with the stored pick: the switch proceeds through
        # the freshness gate (entries served by phase 2 are age-0).
        h = self._harness(temp_home)
        view = {
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        }
        outcome, fetch_sets = self._two_phase_tick(h, view, view)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert {"1", "2", "3"} in fetch_sets

    def test_two_phase_refetch_reranks_to_fresh_best(self, temp_home):
        # Phase 2 is a full re-rank, not a yes/no check on the provisional
        # target: #2 stays eligible on fresh data, but #3 now resets sooner
        # and must win.
        h = self._harness(temp_home)
        stored = {
            "1": _usage7(20, 20, _R_LATEST),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATER),
        }
        fresh = {
            "1": _usage7(20, 20, _R_LATEST),
            "2": _usage7(10, 10, _R_LATER),    # still sooner than active
            "3": _usage7(10, 10, _R_SOON),     # but #3 is now soonest
        }
        outcome, _ = self._two_phase_tick(h, stored, fresh)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_threshold_crossed_in_phase_two_holds_then_escapes_next_tick(
        self, temp_home
    ):
        # Deliberate design pin: phase 2 never re-classifies the trigger
        # mid-tick. When the fresh active is over the threshold with no
        # strictly-sooner candidate, the tick holds; the NEXT tick classifies
        # at-limit and escapes normally (no freshness gate on escapes).
        h = self._harness(temp_home)
        stored = {
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        }
        fresh = {
            "1": _usage7(100, 20, _R_LATER),   # crossed while the snapshot aged
            "2": _usage7(10, 10, _R_LATEST),   # no longer strictly sooner
            "3": _usage7(10, 10, _R_LATEST),
        }
        outcome, fetch_sets = self._two_phase_tick(h, stored, fresh)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert not any(isinstance(e, SwitchEvent) for e in h.events)
        assert {"1", "2", "3"} in fetch_sets
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["already-consuming-soonest"]
        h.events.clear()
        outcome = h.tick_with_usage(fresh)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "at-limit"


class TestConsumeFirstDepartureRecordsItsOwnTrigger:
    """A `consume-first` departure's phase-2 refetch can write
    `(leftHeadroom, leftRecoveryAt) = (None, None)`, the exact snapshot
    shape a `failover` departure writes -- whenever the refetched active row
    has a `pct` but is otherwise unmeasurable in the SAME tick its weekly
    reset is known (`account_headroom` needs a numeric `pct`;
    `_seven_day_reset_ts` needs only `resets_at` -- one row can satisfy one
    and not the other, exactly what the normalizer emits for
    `utilization: null`). `_left_account_recovered` then infers the trigger
    from the two nulls and runs the FAILOVER legs (landing floor + recovery)
    on what was really an ORDINARY departure (dominance + self-improvement +
    recovery) -- and the two branches disagree.

    Fleet: barred peer 11 pts, active 5 pts, no resets anywhere. Failover's
    landing floor (`h > 100 - threshold` = 10) releases at 11. Ordinary's
    dominance ratio (`h > active*2+3` = 13) does not, self-improvement has
    no baseline to diff against (`leftHeadroom` genuinely unmeasured), and
    the recovery leg is inf-vs-inf. The two branches give OPPOSITE answers
    on the identical (None, None) snapshot -- only which trigger produced it
    tells them apart, and that is exactly the bit `leftTrigger` records.
    """

    def test_the_null_snapshot_answers_differently_by_recorded_trigger(self):
        from claude_swap.autoswitch import AutoSwitchEngine
        from claude_swap.settings import AutoSwitchSettings

        class Fake(AutoSwitchEngine):
            def __init__(self):
                self._models = ()

        e = Fake()
        settings = AutoSwitchSettings()
        now = 1_000_000.0
        usage = {"1": _usage(89.0), "2": _usage(95.0)}   # peer 11 pts, active 5 pts
        headroom = {"1": 11.0}

        failover_state = {
            "lastSwitchFrom": "1",
            "leftHeadroom": None,
            "leftRecoveryAt": None,
            "leftTrigger": "failover",
        }
        consume_first_state = {
            "lastSwitchFrom": "1",
            "leftHeadroom": None,
            "leftRecoveryAt": None,
            "leftTrigger": "consume-first",
        }

        failover_recovered = e._left_account_recovered(
            failover_state, usage, headroom, 5.0, settings, now, "2"
        )
        ordinary_recovered = e._left_account_recovered(
            consume_first_state, usage, headroom, 5.0, settings, now, "2"
        )
        assert failover_recovered is True, (
            "a real failover departure's landing floor (h > 10) releases "
            "on an 11-point peer with no baseline to diff against"
        )
        assert ordinary_recovered is False, (
            "a consume-first departure's dominance leg (h > active*2+3=13) "
            "does not clear at 11 points, and there is no leftHeadroom "
            "baseline to self-improve against -- must hold, not borrow the "
            "failover branch's more permissive landing floor"
        )

    def test_pre_upgrade_null_snapshot_without_leftTrigger_still_infers_failover(
        self,
    ):
        """Backward compatibility: a record written before `leftTrigger`
        existed has no such key. Must fall back to the old two-null
        inference (failover) rather than crash or silently misclassify."""
        from claude_swap.autoswitch import AutoSwitchEngine
        from claude_swap.settings import AutoSwitchSettings

        class Fake(AutoSwitchEngine):
            def __init__(self):
                self._models = ()

        e = Fake()
        settings = AutoSwitchSettings()
        now = 1_000_000.0
        usage = {"1": _usage(89.0), "2": _usage(95.0)}
        headroom = {"1": 11.0}
        legacy_state = {
            "lastSwitchFrom": "1",
            "leftHeadroom": None,
            "leftRecoveryAt": None,
            # no "leftTrigger" key
        }
        recovered = e._left_account_recovered(
            legacy_state, usage, headroom, 5.0, settings, now, "2"
        )
        assert recovered is True, (
            "no leftTrigger recorded -> fall back to the pre-I-B inference "
            "(both null -> failover), same as before this fix"
        )

    def test_a_legacy_record_with_a_real_leftHeadroom_is_never_forced_through_the_failover_legs(
        self,
    ):
        """The fallback must only trigger on the OLD (None, None) shape, not
        unconditionally. A legacy record (no `leftTrigger`) with a REAL
        `leftHeadroom` is unambiguous -- it is an ordinary-path departure by
        construction (only `_perform`'s (None, None) write for failover ever
        leaves both null) -- and must still take the ordinary legs, not the
        more permissive failover landing floor, regardless of whether some
        future change makes the failover branch unconditional."""
        from claude_swap.autoswitch import AutoSwitchEngine
        from claude_swap.settings import AutoSwitchSettings

        class Fake(AutoSwitchEngine):
            def __init__(self):
                self._models = ()

        e = Fake()
        settings = AutoSwitchSettings()
        now = 1_000_000.0
        usage = {"1": _usage(89.0), "2": _usage(95.0)}  # peer 11 pts, active 5 pts
        headroom = {"1": 11.0}
        legacy_state_real_headroom = {
            "lastSwitchFrom": "1",
            "leftHeadroom": 10.0,
            "leftRecoveryAt": None,
            # no "leftTrigger" key
        }
        recovered = e._left_account_recovered(
            legacy_state_real_headroom, usage, headroom, 5.0, settings, now, "2"
        )
        assert recovered is False, (
            "leftHeadroom=10.0 is a real (non-null) baseline, so this is "
            "unambiguously an ORDINARY departure -- the failover landing "
            "floor (h > 10, which 11 clears) must NOT decide this; the "
            "ordinary legs (dominance h > active*2+3=13, self-improvement "
            "h >= left+3=13) both fail at h=11 and must hold"
        )

    def test_end_to_end_consume_first_departure_records_its_own_trigger(
        self, temp_home
    ):
        """Drive a real consume-first departure through `_perform` and read
        the persisted state back: `leftTrigger` must be `"consume-first"`,
        not silently absent."""
        h = EngineHarness(temp_home, strategy="consume-first")
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        out = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
        })
        assert out is TickOutcome.SWITCHED
        state = h.engine._read_state()
        assert state.get("leftTrigger") == "consume-first", (
            f"expected leftTrigger='consume-first', got {state.get('leftTrigger')!r}"
        )

    def test_end_to_end_failover_departure_records_its_own_trigger(
        self, temp_home
    ):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        out = None
        for _ in range(3):
            out = h.tick_with_usage({"1": None, "2": _usage(4)})
            h.clock.advance(60.0)
        assert out is TickOutcome.SWITCHED
        state = h.engine._read_state()
        assert state.get("leftTrigger") == "failover", (
            f"expected leftTrigger='failover', got {state.get('leftTrigger')!r}"
        )


class TestEveryAccountAboveThreshold:
    """With nothing below the threshold, go to whatever comes back soonest.

    The state that motivated this was measured, not imagined: all three
    accounts' 5-hour windows at 100/99/95%, threshold 90. Every candidate
    failed the "landing must be healthy" gate, so the engine sat still while
    the active account burned to 100% and Claude Code took a hard session
    limit — with a peer whose window reset in 8 minutes never tried. Claude
    Code's own retry timer is driven by the rate-limit headers it already
    received, so once that limit lands no credential swap can shorten it; the
    only cure is not to arrive there.

    Below the threshold nothing changes: a single healthy peer still wins the
    normal way, and the hysteresis margin still keeps two near-line accounts
    from ping-ponging.
    """

    def _at(self, harness, seconds: float) -> str:
        from datetime import datetime, timezone

        return (
            datetime.fromtimestamp(harness.clock.now + seconds, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def test_moves_to_the_soonest_recovering_account(self, harness):
        """The measured shape: active 99, peers 100 and 95. Account 3 is the
        only one both viable and soon, and it is where we must land."""
        outcome = harness.tick_with_usage({
            "1": _usage(99, self._at(harness, 3600 * 2)),   # active, back in 2h
            "2": _usage(100, self._at(harness, 600)),       # at limit — never a target
            "3": _usage(95, self._at(harness, 480)),        # back in 8 minutes
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_soonest_wins_over_most_headroom(self, harness):
        """Ranking flips in this state: the usual "most headroom" pick is the
        wrong one when every account is nearly spent — what matters is which
        one can work again first."""
        outcome = harness.tick_with_usage({
            "1": _usage(99, self._at(harness, 3600)),
            "2": _usage(91, self._at(harness, 3600 * 3)),  # most headroom, latest back
            "3": _usage(97, self._at(harness, 300)),       # least headroom, soonest back
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_a_single_healthy_peer_still_wins_normally(self, harness):
        """The escape must not fire while an ordinary target exists."""
        outcome = harness.tick_with_usage({
            "1": _usage(99, self._at(harness, 3600)),
            "2": _usage(95, self._at(harness, 60)),   # soonest, but still spent
            "3": _usage(20, self._at(harness, 3600 * 5)),  # healthy
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_below_threshold_is_untouched(self, harness):
        """Nothing about the ordinary below-threshold path changes."""
        outcome = harness.tick_with_usage({
            "1": _usage(50), "2": _usage(10), "3": _usage(10),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert harness.active_number() == 1

    def test_all_at_limit_still_reports_exhausted(self, harness):
        """h <= 0 is still never a target: with everything truly maxed there
        is nowhere to go and the exhausted path must still own that case."""
        outcome = harness.tick_with_usage({
            "1": _usage(100, self._at(harness, 600)),
            "2": _usage(100, self._at(harness, 300)),
            "3": _usage(100, self._at(harness, 900)),
        })
        assert outcome is TickOutcome.BLOCKED
        assert harness.active_number() == 1

    def test_unknown_reset_sorts_last_not_first(self, harness):
        """A candidate whose reset nobody knows must not masquerade as
        'back immediately' and beat a measured, genuinely imminent one."""
        outcome = harness.tick_with_usage({
            "1": _usage(99, self._at(harness, 3600)),
            "2": _usage(95),                          # no resets_at at all
            "3": _usage(97, self._at(harness, 600)),  # known, soon
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_does_not_flap_between_two_near_equal_accounts(self, harness):
        """The escape relaxes the percentage-point hysteresis, so it owes the
        anti-flap guarantee on its own axis: two accounts whose windows roll
        over at nearly the same time must not trade places forever."""
        a = self._at(harness, 600)
        b = self._at(harness, 660)  # 60s apart — inside RECOVERY_HYSTERESIS_S
        first = harness.tick_with_usage({
            "1": _usage(99, a), "2": _usage(98, b), "3": _usage(100, a),
        })
        assert first is TickOutcome.BLOCKED, "60s sooner is not worth a switch"
        assert harness.active_number() == 1

    def test_a_meaningfully_sooner_account_still_wins(self, harness):
        """The margin must not be so wide it swallows the real case."""
        outcome = harness.tick_with_usage({
            "1": _usage(99, self._at(harness, 3600)),
            "2": _usage(98, self._at(harness, 600)),  # an hour sooner
            "3": _usage(100, self._at(harness, 60)),  # at limit — not a target
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2

    def test_consume_first_gets_the_same_anti_flap_guard(self, temp_home):
        """The escape must not depend on which strategy is configured.

        `if consume_first:` used to catch first, so a consume-first user
        reached the ranking (soonest binding recovery) without ever passing
        the recovery-hysteresis gate — filtering on one axis while sorting on
        another. Two accounts whose windows roll over a minute apart could
        then trade places forever.
        """
        h = EngineHarness(temp_home, strategy="consume-first")
        h.seed(1, "a@example.com"); h.seed(2, "b@example.com")
        h.seed(3, "c@example.com"); h.make_live("a@example.com", 1)
        a = self._at(h, 600)
        b = self._at(h, 660)  # 60s apart — inside RECOVERY_HYSTERESIS_S
        outcome = h.tick_with_usage({
            "1": _usage(99, a), "2": _usage(98, b), "3": _usage(100, a),
        })
        assert outcome is TickOutcome.BLOCKED, (
            "consume-first skipped the recovery hysteresis"
        )
        assert h.active_number() == 1

    def test_at_limit_trigger_still_ignores_the_landing_rule(self, harness):
        """at-limit and failover skip the whole proactive block. The escape
        must not have made the active account's 100% case *narrower* — an
        account with real headroom still wins there regardless of resets."""
        outcome = harness.tick_with_usage({
            "1": _usage(100, self._at(harness, 60)),   # active, at limit
            "2": _usage(30, self._at(harness, 86400)),  # healthy but far reset
            "3": _usage(95, self._at(harness, 120)),    # soon but spent
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2, (
            "at-limit must still take the account with real headroom"
        )
        sw = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "at-limit"


class TestRecoveryIsUsefulEitherClause:
    """The `or active_recovery_ts` leg of `_recovery_is_useful` had no
    killer in the full suite, even through `tick()`.

    `test_a_pair_straddling_the_horizon_does_not_ping_pong` (the branch's own
    headline test for this clause) is masked by the no-return bar: 5 of its 6
    ticks are BLOCKED by the bar before `_recovery_is_useful` is ever asked,
    so removing `either` and leaving `cand-only` does not change that test's
    outcome. `either` differs from `cand-only` in exactly one of the four
    (candidate inside/outside horizon) x (active inside/outside horizon)
    combinations: `cand-outside / act-inside`. This drives that quadrant
    directly, at the unit level, bypassing the bar entirely.
    """

    def test_the_active_alone_being_inside_the_horizon_is_enough(self):
        """cand-outside / act-inside: `either` says True, `cand-only` says
        False. Past `SPENT_HEADROOM_PCT` on both sides, so the all-spent
        escape hatch at the top of the function does not pre-empt this."""
        now = 1_000_000.0
        cand_recovery_ts = now + RECOVERY_HORIZON_S + 3600.0   # OUTSIDE
        active_recovery_ts = now + 1800.0                       # INSIDE
        assert _recovery_is_useful(
            cand_recovery_ts, active_recovery_ts,
            active_headroom=50.0, best_candidate_headroom=50.0, now=now,
        ) is True, (
            "the active's own reset is inside the horizon, which must rank "
            "by recovery even though the candidate's reset is not — this is "
            "the `either` clause, and `cand-only` would answer False here"
        )

    def test_the_control_neither_inside_falls_back_to_headroom(self):
        """Control: both outside the horizon -> ranks by headroom (False)."""
        now = 1_000_000.0
        cand_recovery_ts = now + RECOVERY_HORIZON_S + 3600.0
        active_recovery_ts = now + RECOVERY_HORIZON_S + 7200.0
        assert _recovery_is_useful(
            cand_recovery_ts, active_recovery_ts,
            active_headroom=50.0, best_candidate_headroom=50.0, now=now,
        ) is False, (
            "premise: with neither reset inside the horizon, the function "
            "must fall back to headroom — control for the test above"
        )


class TestRecoveryHorizon:
    """The recovery escape must not spend real headroom on a distant reset.

    #202's rule ("go where quota returns first") was measured on minutes-scale
    resets and shipped with no upper bound, so it applied identically days
    out — measured live, 9 points of headroom traded for 2 on a reset nobody
    reaches today. Past the horizon, ranking returns to headroom.
    """

    def _at(self, harness, seconds: float) -> str:
        from datetime import datetime, timezone

        return (
            datetime.fromtimestamp(harness.clock.now + seconds, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def test_a_minutes_away_reset_still_wins(self, harness):
        """The #202 design case is unchanged: an 8-minute wait is worth 9
        points of headroom."""
        outcome = harness.tick_with_usage({
            "1": _usage(91, self._at(harness, 7200)),   # active, 9 left, back in 2h
            "2": _usage(94, self._at(harness, 1800)),
            "3": _usage(98, self._at(harness, 480)),    # back in 8 minutes
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_a_days_away_reset_does_not_buy_headroom(self, harness):
        """The measured live shape. Every reset is days out, so ranking falls
        back to headroom and the account with 9 points left keeps the work."""
        outcome = harness.tick_with_usage({
            "1": _usage(91, self._at(harness, 109 * 3600)),  # active, 9 left
            "2": _usage(94, self._at(harness, 80 * 3600)),
            "3": _usage(98, self._at(harness, 50 * 3600)),   # 2 left, soonest
        })
        assert harness.active_number() == 1, (
            "traded 9 points of headroom for 2 on a reset nobody reaches today"
        )

    def test_an_unreadable_peer_does_not_veto_the_spent_check(
        self, temp_home
    ):
        """The spent check ranks the CANDIDATES, not every account in `usage`.

        `headroom` is keyed off `usage`, which carries a row for accounts the
        loop can never pick — a sentinel (unreadable credential, keychain
        locked) yields `None` headroom. Testing `headroom.values()` let one
        such row make `all(...)` False forever, so the spent escape could not
        fire: measured, three accounts at 99% days out and the engine parked
        on the one resetting LAST. That is the bug SPENT_HEADROOM_PCT exists
        to prevent, reintroduced through the iteration set.
        """
        h = EngineHarness(temp_home)
        for n, e in ((1, "a@example.com"), (2, "b@example.com"),
                     (3, "c@example.com"), (4, "d@example.com")):
            h.seed(n, e)
        h.make_live("a@example.com", 1)

        outcome = h.tick_with_usage({
            "1": _usage(99, self._at(h, 109 * 3600)),  # active, resets LAST
            "2": _usage(99, self._at(h, 80 * 3600)),
            "3": _usage(99, self._at(h, 50 * 3600)),   # soonest
            "4": USAGE_TOKEN_EXPIRED,                  # sentinel: headroom None
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3, (
            "an unreadable peer vetoed the spent check and parked the engine "
            "on the account resetting last"
        )

    def test_an_unreadable_peer_does_not_forge_headroom_for_the_spent_check(
        self, temp_home
    ):
        """Counting an unreadable candidate's headroom as 100.0 instead of
        excluding it turns the all-spent gate off for the whole fleet.

        `best_candidate_headroom` filters `None` (unreadable) rows out of the
        max — the sibling test above proves exclusion doesn't VETO the check.
        This proves the other failure mode: if an unreadable row were instead
        counted as a maximal 100.0, `best_candidate_headroom` would read 100
        even though every REAL candidate is spent, so the all-spent branch
        of `_recovery_is_useful` goes false and the far-out-reset fallback
        below it (which requires the candidate to be no worse than the
        active) excludes a candidate that is legitimately better on the
        recovery axis alone.

        Active and the one real candidate are both spent (<= SPENT_HEADROOM_
        PCT), the candidate resets meaningfully sooner than the active but
        both resets are far past RECOVERY_HORIZON_S (so the per-pair "back
        soon" fallback in `_recovery_is_useful` cannot rescue it either), and
        the candidate holds LESS raw headroom than the active (so the
        separate spent-headroom fallback, which requires `h >= active_
        headroom`, does not re-admit it). Only the all-spent branch treating
        `best_candidate_headroom` as the real 2.0 (not a forged 100.0) lets
        this candidate through.
        """
        h = EngineHarness(temp_home)
        for n, e in ((1, "a@example.com"), (2, "b@example.com"),
                     (3, "c@example.com")):
            h.seed(n, e)
        h.make_live("a@example.com", 1)

        outcome = h.tick_with_usage({
            "1": _usage(97.5, self._at(h, 500 * 3600)),  # active, 2.5 pts
            "2": _usage(98.0, self._at(h, 490 * 3600)),  # 2.0 pts, sooner reset
            "3": USAGE_TOKEN_EXPIRED,                     # sentinel: headroom None
        })
        assert outcome is TickOutcome.SWITCHED, (
            "the real candidate is spent but resets meaningfully sooner than "
            "the active; forging its unreadable sibling's headroom as 100.0 "
            "turns off the all-spent recovery escape that should have picked "
            "it"
        )
        assert h.active_number() == 2

    def test_a_weekly_bound_active_does_not_refuse_a_peer_back_in_minutes(
        self, harness
    ):
        """The horizon is asked PER CANDIDATE, not once on the active.

        An active bound by its WEEKLY window sits days out while a peer's
        five-hour window returns in minutes. Asking the active refused that
        peer — the #202 case this horizon is supposed to preserve. The
        existing tests never caught it because they populate only a 5h
        window, so the active's reset and the candidates' always moved
        together.
        """
        outcome = harness.tick_with_usage({
            # active: 5h fine, WEEKLY at 96% resetting 109h out — days.
            "1": _usage7(10, 96, self._at(harness, 109 * 3600)),
            # peer: binding 5h window back in 8 minutes.
            "2": _usage(98, self._at(harness, 480)),
            "3": _usage(99, self._at(harness, 90 * 3600)),
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2, (
            "refused a peer returning in 8 minutes because the ACTIVE was "
            "weekly-bound"
        )

    def test_an_unknown_active_reset_keeps_the_headroom(self, harness):
        """`inf` means unknown OR already elapsed — not 'rank by reset'.

        It used to keep the recovery axis, which re-armed the exact trade the
        horizon forbids: measured, an active with 9 points and no `resets_at`
        moved to a peer with 1 point resetting 50h out. No evidence that a
        sooner reset helps is a reason to keep the headroom, not spend it.
        """
        outcome = harness.tick_with_usage({
            "1": _usage(91),                                # active, no reset
            "2": _usage(98, self._at(harness, 80 * 3600)),
            "3": _usage(99, self._at(harness, 50 * 3600)),  # soonest, 1 left
        })
        assert harness.active_number() == 1, (
            "traded 9 points for 1 because the active's reset was unknown"
        )
        assert outcome is not TickOutcome.SWITCHED

    def test_a_peer_with_real_headroom_still_wins_past_the_horizon(self, harness):
        """Falling back to headroom is not "never move": a peer holding
        materially more quota is still the right landing, days-away or not."""
        outcome = harness.tick_with_usage({
            "1": _usage(97, self._at(harness, 50 * 3600)),   # active, 3 left
            "2": _usage(91, self._at(harness, 109 * 3600)),  # 9 left
            "3": _usage(98, self._at(harness, 60 * 3600)),
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2


class TestTheHorizonDoesNotDiscardWhatItAlreadyKnows:
    """Two regressions from carrying the horizon into the ranking.

    Both are cases where the PR had the right answer in hand and dropped it —
    base 9f35426 gets them right, so neither is inherited.
    """

    def _at(self, harness, seconds: float) -> str:
        from datetime import datetime, timezone

        return (
            datetime.fromtimestamp(harness.clock.now + seconds, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def test_equal_headroom_past_the_horizon_takes_the_sooner_reset(self, harness):
        """The tier-1 key hard-coded ``0.0`` where ``recovery_ts`` belongs.

        Two peers with IDENTICAL headroom, both past the horizon, one returning
        in 5h and one in 500h. With the second slot zeroed the tie falls
        through to sequence order, so which account is chosen depends on which
        slot number it happens to occupy:

            near is acct 3  ->  base picks 3 (5h),  head picked 2 (500h)

        `recovery_ts` is already computed at that point and is strictly better
        than list order at zero cost. Headroom still outranks it — the tier
        byte separates the two axes, and `-h` still comes first within tier 1.
        """
        out = harness.tick_with_usage({
            "1": _usage(96, self._at(harness, 300 * 3600)),   # active, 4 pts
            "2": _usage(92, self._at(harness, 500 * 3600)),   # 8 pts, LAST
            "3": _usage(92, self._at(harness, 5 * 3600)),     # 8 pts, soonest
        })
        assert out is TickOutcome.SWITCHED
        assert harness.active_number() == 3, (
            "equal headroom, and the 5h reset lost to the 500h one on slot order"
        )

    def test_a_peer_worth_having_is_not_filtered_out_of_the_worth_having_check(
        self, harness
    ):
        """The floor must not exclude a peer that plainly has quota.

        `best_candidate_headroom` was scoped by
        `active_headroom x HORIZON_HEADROOM_RATIO` — but that constant is an
        ANTI-FLAP MARGIN, not a "worth having" cutoff. A peer at
        2x-minus-epsilon the active's headroom is very much worth having; it
        merely fails this tick's margin.

        With every candidate below the floor, `default=0.0` makes
        `best_candidate_headroom` 0.0, which SATISFIES the spent clause — so
        the clause fires for everybody, the horizon check is never reached, and
        ranking falls to soonest reset regardless of headroom. The engine then
        takes a nearly-empty account over one holding 60x more:

            active   3.00 pts, 500h     peerA 5.99 pts, 400h
            peerB    0.10 pts, 200h  <- chosen

        Three-way: base 9f35426 also lands on peerB, but 0457cb0 (this PR
        before the veto-scope fix) holds the active. So the fix reintroduced
        base's answer in a band the commit before it had already made safe.

        Asserted as "does not take the nearly-empty account". Whether it takes
        peerA or holds is the ANTI-FLAP margin's call, and at 5.99 against a
        6.00 margin holding is correct — that is a separate question from
        whether peerA counts as quota existing, which is what this pins.
        """
        out = harness.tick_with_usage({
            "1": _usage(97.00, self._at(harness, 500 * 3600)),  # active, 3.00
            "2": _usage(94.01, self._at(harness, 400 * 3600)),  # 5.99
            "3": _usage(99.90, self._at(harness, 200 * 3600)),  # 0.10
        })
        assert harness.active_number() != 3, (
            "took the 0.10-point account over one holding 5.99 — the floor "
            "excluded the peer that made the spent clause false, and an empty "
            "max reads as 'nothing is worth having'"
        )
        assert out is not TickOutcome.SWITCHED or harness.active_number() == 2

    def test_an_unchoosable_peer_does_not_veto_the_reset_ranking(self, harness):
        """``best_candidate_headroom`` counted a candidate the ranking cannot pick.

        The spent check asks "is anything worth having?" of the BEST candidate.
        A peer holding 3.05 points is above ``SPENT_HEADROOM_PCT``, so the
        answer is no for everybody — yet that peer cannot itself be chosen,
        because 3.05 < 3.0 x HORIZON_HEADROOM_RATIO fails the ratio gate.
        Nothing qualifies, and the engine parks on the account that returns
        LAST:

            active   3.00 pts, resets in 200h   <- stays here
            peer     3.00 pts, resets in  10h   <- 190h sooner, refused
            vetoer   3.05 pts, resets in 500h   <- unchoosable, decides

        Measured against base: base switches at 3.05 and this branch blocks.
        The veto band is (SPENT_HEADROOM_PCT, active x RATIO], up to 3 points
        wide, so it is not an edge case in the endgame this code is for.

        ``test_an_unreadable_peer_does_not_veto_the_spent_check`` pins the same
        shape for an UNREADABLE peer; a readable one 0.05 points over the line
        does the same damage.
        """
        out = harness.tick_with_usage({
            "1": _usage(97.0, self._at(harness, 200 * 3600)),   # active, 3 pts
            "2": _usage(97.0, self._at(harness, 10 * 3600)),    # 3 pts, sooner
            "3": _usage(96.95, self._at(harness, 500 * 3600)),  # 3.05 pts
        })
        assert out is TickOutcome.SWITCHED
        assert harness.active_number() == 2, (
            "a peer that cannot be chosen vetoed the ranking for everyone"
        )


class TestHorizonAxisDoesNotFlap:
    """Past the horizon the headroom axis needs its own anti-flap margin.

    The first cut required only *strictly more* headroom, which is no margin
    at all: one point is enough to move, and the account we move to burns that
    point back within a poll or two. Measured live on 2026-07-30, four switches
    in 35 minutes, each buying one point and each costing a credential rewrite:

        17:28  acct 1 (5% left) -> acct 2 (6%)
        17:49  acct 2 (4% left) -> acct 1 (5%)
        17:54  acct 1           -> acct 2
        18:03  acct 2 (2% left) -> acct 1 (3%)

    The ordinary path uses ``hysteresis_pct`` (10 points), but that is
    unmeetable here by construction — everything is within a few points of its
    limit, so requiring ten would park the engine and let it ride into the
    wall, which is the failure #202 exists to prevent.

    A RATIO is the right unit in the endgame: with two points left, what
    matters is how many times more runway the target has, not how many points.
    Requiring the target to hold ``HORIZON_HEADROOM_RATIO`` times the active
    account's headroom makes the move one-way by construction — the reverse
    would need the new active to fall to a quarter of what it just beat.
    """

    def _at(self, harness, seconds: float) -> str:
        from datetime import datetime, timezone

        return (
            datetime.fromtimestamp(harness.clock.now + seconds, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def _days_out(self, harness, hours):
        """Absolute reset ANCHORED on first use, per (harness, hours).

        `_at(harness, seconds)` computes `now + seconds` from the CURRENT
        clock every call. Called again after `harness.clock.advance(...)`
        with the same `hours`, that keeps producing "N hours from NOW",
        which never approaches -- a real `resets_at` is a fixed epoch, and
        the remaining time to it shrinks as the clock advances. Memoizing
        the first computed timestamp per (harness, hours) makes repeated
        calls in a multi-tick loop return the SAME absolute instant, so it
        genuinely draws nearer as the test's clock advances.
        """
        cache = self.__dict__.setdefault("_days_out_cache", {})
        key = (id(harness), hours)
        if key not in cache:
            cache[key] = self._at(harness, hours * 3600)
        return cache[key]

    def test_a_fixed_reset_crosses_into_the_horizon_as_the_clock_advances(
        self, harness
    ):
        """`_days_out` must return a FIXED absolute instant, not
        "N hours from whenever this is called."

        Both accounts start above the threshold (`all_above`), the peer's
        reset fixed 5h out — outside `RECOVERY_HORIZON_S` (4h) — so the
        first tick ranks by headroom, where the peer's one extra point does
        not clear `HORIZON_HEADROOM_RATIO` and the tick holds. Advancing the
        clock 90 minutes brings that SAME fixed reset to 3.5h away — inside
        the horizon — so the second tick must rank by recovery instead and
        switch. A `_days_out` that recomputes "N hours from now" on every
        call would keep reporting the peer's reset as exactly 5h out
        forever, and the horizon would never be crossed.
        """
        outcome1 = harness.tick_with_usage({
            "1": _usage(95, self._days_out(harness, 400)),   # active, far reset
            "2": _usage(94, self._days_out(harness, 5)),     # peer, 5h out
        })
        assert outcome1 is not TickOutcome.SWITCHED, (
            "premise: 5h is outside RECOVERY_HORIZON_S and one point of "
            "headroom is not enough to qualify on its own"
        )
        harness.clock.advance(90 * 60.0)

        outcome2 = harness.tick_with_usage({
            "1": _usage(95, self._days_out(harness, 400)),
            "2": _usage(94, self._days_out(harness, 5)),     # SAME fixed reset
        })
        assert outcome2 is TickOutcome.SWITCHED, (
            "the peer's fixed reset is now 3.5h away, inside the horizon — "
            "a `_days_out` that recomputes from `now` every call would still "
            "report 5h out and never cross it"
        )
        assert harness.active_number() == 2

    def test_one_point_of_headroom_does_not_move(self, harness):
        """The measured flap: 95% active against a 94% peer, both days out."""
        outcome = harness.tick_with_usage({
            "1": _usage(95, self._days_out(harness, 109)),   # active, 5 left
            "2": _usage(94, self._days_out(harness, 80)),    # 6 left
            "3": _usage(99, self._days_out(harness, 50)),
        })
        assert harness.active_number() == 1, (
            "moved for one point of headroom; the target burns it back and the "
            "engine ping-pongs (measured: 4 switches in 35 minutes)"
        )
        assert outcome is not TickOutcome.SWITCHED

    def test_the_return_leg_is_blocked_too(self, harness):
        """Same shape with the roles reversed — symmetric, so neither leg runs."""
        outcome = harness.tick_with_usage({
            "1": _usage(96, self._days_out(harness, 109)),   # active, 4 left
            "2": _usage(95, self._days_out(harness, 80)),    # 5 left
            "3": _usage(99, self._days_out(harness, 50)),
        })
        assert harness.active_number() == 1
        assert outcome is not TickOutcome.SWITCHED

    def test_a_pair_straddling_the_horizon_does_not_ping_pong(self, harness):
        """Each guard is one-way on ITS OWN axis — but the axis itself flips.

        ``_recovery_is_useful`` reads the ACTIVE account's headroom and the
        CANDIDATE's reset, and a switch swaps both operands. So a pair that
        straddles the horizon takes the recovery gate going out and the
        headroom gate coming back, and neither guard ever sees the other leg:

            acct 1   8 points, reset 109h out   (past the horizon)
            acct 2   3 points, reset 3.5h out   (inside it)

            active=1 -> candidate 2 is inside  -> recovery axis  -> 3.5h < 109h
            active=2 -> candidate 1 is outside -> headroom axis  -> 8 >= 3*2

        Both legs qualify on frozen inputs, so the engine rewrites credentials
        every cooldown until the sooner reset actually lands. Every other test
        in this class ticks ONCE, which is why the pair went unseen.
        """
        r_far = self._days_out(harness, 109)
        r_near = self._days_out(harness, 3.5)
        seen = []
        for _ in range(6):
            harness.tick_with_usage({
                "1": _usage(92, r_far),    # 8 points, returns days out
                "2": _usage(97, r_near),   # 3 points, returns inside 4h
            })
            seen.append(harness.active_number())
            harness.clock.advance(301.0)   # past the 300s cooldown
        assert len(set(seen)) == 1, (
            f"cross-axis oscillation: active trace {seen} — each leg passes "
            "the guard belonging to the OTHER leg's axis"
        )

    def test_the_spent_fallback_needs_a_meaningfully_sooner_reset(self, harness):
        """The fallback is bounded by the SAME hysteresis the recovery axis uses.

        Its other two guards are pinned by the tests above (dropping the spent
        gate reddens `test_one_point_of_headroom_does_not_move` and
        `test_the_return_leg_is_blocked_too`; dropping `h >= active` reddens
        `test_a_peer_worth_having_is_not_filtered_out_of_the_worth_having_check`).
        The margin was the one nothing killed — measured, replacing
        `< active_recovery_ts - RECOVERY_HYSTERESIS_S` with a bare
        `< active_recovery_ts` left all 168 tests in this file green.

        Exhaustive 2- and 3-account sweep over headroom x reset, 16200 shapes:
        exactly 42 change answer, all of the shape below.

            acct 1   2.5 points, reset 500.02h out   (active)
            acct 2   4.0 points, reset 500.00h out   (72s sooner)

            with the margin     no move, both legs
            without it          active=1 moves to 2; active=2 holds

        One-way, so not a flap — a credential rewrite bought with 72 seconds
        of earlier return, on a pair that both come back in three weeks. The
        margin is what makes "sooner" mean sooner enough to be worth the write.
        """
        outcome = harness.tick_with_usage({
            "1": _usage(97.5, self._days_out(harness, 500.02)),  # active, 2.5 left
            "2": _usage(96.0, self._days_out(harness, 500.0)),   # 4 left, 72s sooner
        })
        assert harness.active_number() == 1, (
            "moved for a 72-second-sooner reset three weeks out — inside "
            "RECOVERY_HYSTERESIS_S, which is what bounds the write rate"
        )
        assert outcome is not TickOutcome.SWITCHED

    def test_the_tier_byte_puts_a_returning_peer_ahead_of_a_distant_one(
        self, harness
    ):
        """`(0, ...)` before `(1, ...)` — the tier prefix itself, not its tail.

        Both existing tier tests compare candidates WITHIN one tier, so the
        byte cancels and neither pins it. Collapsing it to a flat key left the
        suite green.

        A candidate returning inside the horizon beats one that does not,
        whatever its headroom: acct 2 is nearly spent but works again in an
        hour; acct 3 has nine points that never return this session.
        """
        outcome = harness.tick_with_usage({
            "1": _usage(99, self._days_out(harness, 300)),    # active, 1 left
            "2": _usage(98.5, self._days_out(harness, 1)),    # 1.5 left, back in 1h
            "3": _usage(91, self._days_out(harness, 400)),    # 9 left, never
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2, (
            f"landed on {harness.active_number()} — took headroom that never "
            "returns over a peer that works again in an hour"
        )

    def test_the_fallback_breaks_a_reset_tie_by_headroom(self, harness):
        """The fallback key's THIRD slot: `(0, recovery_ts, -h)`.

        `test_the_fallback_ranks_by_reset_not_by_headroom` pins the second
        slot (reset leads). The third was untested — `-h` to `h` left the suite
        green. It needs an actual tie in `recovery_ts` AND both peers routed
        through the fallback, which requires the active to sit exactly at
        SPENT_HEADROOM_PCT so neither peer meets the ratio.
        """
        same = self._days_out(harness, 10)
        outcome = harness.tick_with_usage({
            "1": _usage(97, self._days_out(harness, 300)),   # active, 3.0 left
            "2": _usage(97, same),                            # 3.00 left
            "3": _usage(96.95, same),                         # 3.05 left
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3, (
            f"landed on {harness.active_number()} — at an equal reset the "
            "fallback took the smaller headroom"
        )

    def test_past_the_horizon_headroom_decides_before_the_reset(self, harness):
        """Tier 1 is `(1, -h, recovery_ts)` — headroom leads, reset breaks ties.

        Past the horizon the reset is days out either way, so it cannot be the
        thing that decides; the headroom is the only resource that still does
        work this session. The reset stays in the key so two equal-headroom
        peers do not tie into sequence order.

        Nothing pinned the ORDER: swapping to `(1, recovery_ts, -h)` left the
        whole suite green. The one test that touches the tier uses EQUAL
        headroom (92/92), where both orderings agree — it kills the
        hard-coded-`0.0` mutant and not this one.

        Sweep over 23328 three-account shapes: 2040 change answer. The shape
        below is one, and the trade the reset-first key makes is 2 points of
        headroom for a reset 10 hours sooner, on a pair that both return
        within a day.
        """
        outcome = harness.tick_with_usage({
            "1": _usage(99, self._days_out(harness, 20)),     # active, 1 left
            "2": _usage(98, self._days_out(harness, 10)),     # 2 left, sooner
            "3": _usage(96, self._days_out(harness, 20)),     # 4 left
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3, (
            f"landed on {harness.active_number()} — the reset outranked twice "
            "the headroom, past a horizon where neither reset is near"
        )

    def test_a_burn_walk_settles_instead_of_oscillating(self, harness):
        """A-B-A under burn is the fleet changing regime, not a gate leaking.

        The reviewed concern was that the outbound gate is RELATIVE
        (`h >= active x HORIZON_HEADROOM_RATIO`) while the fallback's is
        ABSOLUTE (`active <= SPENT_HEADROOM_PCT`), so a pair could take one
        gate out and the other back. Measured at the moment of each move,
        only the active burning, both resets past the horizon:

            out    active 2.0 / peer 4.0   headroom axis, 4.0 >= 2.0x2
            back   active 3.0 / peer 2.0   recovery  axis, 10h vs 80h

        Both legs are legitimate on the axis their own state selects: at the
        first the fleet still held real headroom, by the second every account
        is spent, which is the regime the reset axis exists for. Base never
        makes that transition because it refuses the outbound leg too.

        Two candidate constraints were measured and BOTH changed the count by
        zero — requiring the fallback's candidate to be spent, and taking
        `max`/`min` over the pair in `_recovery_is_useful`. The transition is
        in the data, not in the gates, so neither is shipped.

        What must hold is that a walk SETTLES. This one does: two moves in 24
        ticks, then stationary.
        """
        seen = []
        pct = {"1": 96.0, "2": 92.0}          # 4.0 and 8.0 points
        for _ in range(24):
            harness.tick_with_usage({
                "1": _usage(pct["1"], self._days_out(harness, 20)),
                "2": _usage(pct["2"], self._days_out(harness, 80)),
            })
            active = harness.active_number()
            seen.append(active)
            pct[str(active)] = min(99.95, pct[str(active)] + 0.25)   # burn
            harness.clock.advance(301.0)

        moves = [n for i, n in enumerate(seen) if i == 0 or n != seen[i - 1]]
        assert len(moves) <= 2, (
            f"move sequence {moves} — a burn walk that keeps moving is a flap, "
            "whatever axis each leg took"
        )
        assert seen[-4:] == [seen[-1]] * 4, (
            f"active trace {seen} — the walk never settled"
        )

    def test_the_no_return_filter_does_not_block_the_at_limit_escape(
        self, harness
    ):
        """Every sibling anti-flap gate is scoped to the proactive triggers.

        `at-limit` and `failover` skip them by design — there we are escaping a
        dead account, not optimising a return time. The no-return filter ran in
        `_tick_inner` BEFORE the trigger is consulted, so it stripped the
        candidate from those escapes too.

        Measured, 2 accounts, the active exhausted and the peer at full quota:

            base 9f35426   switches on tick 1
            here           switches=0, "no-candidates" every tick, 720 ticks

        Nothing releases it: `lastSwitchFrom` is only rewritten by a successful
        switch, and the field is what prevents the switch. On a 3-account fleet
        it also emits AllExhaustedEvent — a false claim that reaches the user
        as a macOS notification and a critical TUI row while a peer sits at 0%.
        """
        harness.engine._mutate_state(
            lambda st: st.__setitem__("lastSwitchFrom", "2")
        )
        outcome = harness.tick_with_usage({
            "1": _usage(100),      # active, exhausted -> at-limit
            "2": _usage(0),        # the account we left, now at full quota
        })
        assert outcome is TickOutcome.SWITCHED, (
            "the at-limit escape was refused because we had left that account "
            "once — the engine sits on an exhausted account with a peer at 0%"
        )
        assert harness.active_number() == 2

    def test_a_burn_walk_never_returns_to_what_it_left(self, harness):
        """The axis can flip more than once, and nothing bounded how often.

        A previous round measured A-B-A under burn and dismissed it: each leg
        IS legitimate on the axis its own state selects, and base shows 0 only
        because it refuses the outbound leg too. That reasoning holds. The
        conclusion did not — it rested on the walk settling in at most three
        moves, which is a property of the one shape that was measured.

        Measured, only the active burning at 0.5 pts/tick, both resets past
        the horizon, 24 ticks:

            pcts (96,92) resets (20h, 80h)    moves [2, 1]        settles
            pcts (92,92) resets (500h,400h)   moves [1, 2, 1, 2]  does not

        Base on both: a single move. Traced at each leg of the second shape —

            t8   1->2  headroom axis   active 4.0 / best 8.0
            t20  2->1  headroom axis   active 2.0 / best 4.0
            t22  1->2  recovery axis   active 3.0 / best 2.0

        The ratio gate is RELATIVE (`h >= active x 2`) and the spent gate
        ABSOLUTE (`active <= 3.0`), so burn walks the pair across the boundary
        repeatedly and each crossing re-opens a move. Extending to 120 ticks
        stops only because both accounts hit the 99.95 burn cap, so the fourth
        move is not a transient.

        Refusing the account we most recently left bounds it — but identity
        alone has no release, and on a 2-account fleet that is a permanent
        proactive lockout (see the sibling test). Released by asking the
        ranking, the walk is still BOUNDED: it ends, because each return has to
        clear the margin and burn makes that harder every time.

        A trace of the exact moves used to sit here. It has been re-taken three
        times and come back different every time — the walk depends on the
        release, and the release has changed in every round that quoted it. The
        assertion is on SETTLING for the same reason.

        So this asserts a BOUND, not zero returns. Zero was a property of the
        release-less filter, and that property is what made the lockout
        permanent. A walk that ends is the real requirement; the live incident
        this class documents was four moves in 35 minutes and still climbing.
        """
        pct = {"1": 92.0, "2": 92.0}
        seen = []
        # 60, not 24: the settling point moves with fleet size — a longer
        # ring walks further before it comes back — and 24 ticks caught this
        # shape mid-walk.
        for _ in range(60):
            harness.tick_with_usage({
                "1": _usage(pct["1"], self._days_out(harness, 500)),
                "2": _usage(pct["2"], self._days_out(harness, 400)),
            })
            active = harness.active_number()
            seen.append(active)
            pct[str(active)] = min(99.95, pct[str(active)] + 0.5)
            harness.clock.advance(301.0)

        moves = [n for i, n in enumerate(seen) if i == 0 or n != seen[i - 1]]
        # SETTLING is the property, not a move count. `len(moves) <= 4` was
        # true of this 2-account shape and false of every other — the bar
        # refuses only the ONE account left last, so a longer ring walks
        # further before it comes back, and every fleet size settles.
        #
        # The per-size counts that used to be quoted here did not re-measure
        # after the release changed. A count that holds for one fleet size and
        # one release reads as a bound and is neither. What the walk has to do
        # is END.
        assert len(set(seen[-8:])) == 1, (
            f"move sequence {moves} — the walk was still moving in the last "
            "eight ticks, so it does not settle at all"
        )

    def test_a_proactive_move_does_not_lock_out_the_next_one(self, harness):
        """The no-return filter has no release condition on a 2-account fleet.

        `lastSwitchFrom` is written only by a SUCCESSFUL switch, and on two
        accounts the filter removes the only candidate — so the switch that
        would rewrite it can never happen. Self-perpetuating, and persisted:
        it survives a restart and a week of wall clock.

        Reached by ONE ordinary proactive move, no seeded state. After it, the
        peer resets to full and the active keeps burning; every proactive tick
        answers "no-candidates" while a 0% account sits there. The engine
        escapes only at a hard 100%, which is the feature turned off — the
        user hits the limit they were supposed to be switched away from.

        Asserts on the SWITCH, not on the state field: the field being clear
        proves nothing about whether a move can happen, and the scoped filter
        deliberately leaves the field set on the at-limit path.
        """
        assert harness.tick_with_usage({
            "1": _usage(92, self._days_out(harness, 500)),
            "2": _usage(10, self._days_out(harness, 400)),
        }) is TickOutcome.SWITCHED
        assert harness.active_number() == 2
        harness.clock.advance(301.0)

        # The account we left is now fully reset; the new active burns on.
        outcomes = []
        for _ in range(20):
            outcomes.append(harness.tick_with_usage({
                "1": _usage(0, self._days_out(harness, 400)),
                "2": _usage(97, self._days_out(harness, 500)),
            }))
            harness.clock.advance(1801.0)

        assert TickOutcome.SWITCHED in outcomes, (
            f"20 ticks / 10h of outcomes {[o.name for o in outcomes]} — the "
            "peer was at 0% the whole time and the active burned to 97%. The "
            "one proactive move disabled proactive switching permanently."
        )

    def test_an_ordinary_departure_does_not_stall_on_a_weekly_bound_peer(
        self, temp_home
    ):
        """The anti-flap snapshot stalls ORDINARY departures too, not
        just failover, whenever the barred peer is weekly-bound.

        Same root as the failover recovery leg (a release keyed away from
        the ACTIVE), reached without any failover: an ordinary consume-first
        departure records
        `leftHeadroom`/`leftRecoveryAt`, and `_left_account_recovered`'s
        headroom leg (`h >= left_headroom + SPENT_HEADROOM_PCT`) can only
        rise when the barred peer's OWN headroom improves. A peer whose
        headroom is pinned by 7-day utilization (5-hour rollovers do not
        raise it) never improves, and its `resets_at` is a fixed absolute
        that never creeps nearer, so the recovery leg cannot fire either —
        both legs are permanently unsatisfiable even though the peer is
        already 35x better than the active.

        Peer 1's reset (20 days out) stays sooner than active account 2's
        (400 days out) throughout, so the ordinary consume-first
        reset-ordering gate does not exclude it either; only the anti-flap
        snapshot does.

        DISCRIMINATES RELATIONAL FROM ABSOLUTE: the first tick below
        (active_pct=50, active_headroom=50.0) is chosen so that ANY absolute
        floor at or below 70 — the defect class an earlier cut shipped,
        which this test could not tell apart from a relational fix — would
        release the peer immediately (70 clears a `>= floor<=70` bar
        outright), while the relational fix
        needs `70 > 50 x 2 + 3 = 103`, which is false, so it must still hold
        at that first tick. Only once the active has burned enough for the
        RATIO against it (not the peer's own absolute value) to clear does
        the release fire.
        """
        h = EngineHarness(temp_home, strategy="consume-first")
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        assert h.tick_with_usage({
            "1": _usage7(30, 30, self._days_out(h, 20)),   # 70 pts, resets in 20d
            "2": _usage7(50, 50, self._days_out(h, 10)),   # 50 pts, resets SOONER
        }) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        h.clock.advance(301.0)

        outcomes = []
        for active_pct in (50, 80, 90, 95, 98, 99.5, 100):
            outcomes.append(h.tick_with_usage({
                "1": _usage7(30, 30, self._days_out(h, 20)),   # frozen, 70 pts
                "2": _usage7(active_pct, active_pct, self._days_out(h, 400)),
            }))
            h.clock.advance(301.0)

        assert outcomes[0] is not TickOutcome.SWITCHED, (
            f"{[o.name for o in outcomes]} — the very first tick (active at "
            "50%, ratio only 1.4x) already switched. An absolute floor <= 70 "
            "would fire here immediately; the relational fix must not."
        )
        assert TickOutcome.SWITCHED in outcomes[:-1], (
            f"{[o.name for o in outcomes]} — peer 1 held 70 points the whole "
            "time, far ahead of the active, and the engine only returned "
            "once the active hit a hard 100%"
        )

    def test_a_filtered_candidate_does_not_forge_an_all_exhausted_claim(
        self, temp_home
    ):
        """The filter runs BEFORE `truly_exhausted`, so it hides the evidence.

        A healthy peer removed from `oauth_candidates` cannot make the `all()`
        False, and the engine then claims every account is exhausted. The user
        gets a macOS notification and a critical TUI row while that peer sits
        at 0%, and `_blocked_wait_long` stretches the poll interval, so the
        recovery it is wrong about arrives slower too.

        Needs a genuinely spent THIRD account: with only the filtered peer the
        list goes empty and the tick exits at `no-candidates` first — measured,
        3 ticks of `no-candidates` and no event. The false claim needs the
        remaining candidates to be real and all spent, which is the shape a
        user on three accounts actually hits.

        Reached on the proactive path, which the at-limit escape test does not
        cover. Asserts on the EVENT the user sees, not on the candidate list.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)

        assert h.tick_with_usage({
            "1": _usage(92, self._days_out(h, 500)),
            "2": _usage(10, self._days_out(h, 400)),
            "3": _usage(100, self._days_out(h, 300)),
        }) is TickOutcome.SWITCHED
        h.clock.advance(301.0)

        h.events.clear()
        h.tick_with_usage({
            "1": _usage(0, self._days_out(h, 400)),    # the one we left: FULL
            "2": _usage(97, self._days_out(h, 500)),
            "3": _usage(100, self._days_out(h, 300)),  # genuinely spent
        })
        assert not any(isinstance(e, AllExhaustedEvent) for e in h.events), (
            f"events {[type(e).__name__ for e in h.events]} — account 1 is at "
            "0%; the fleet is not exhausted, the filter hid the one peer that "
            "disproves it"
        )

    def test_the_bar_does_not_hide_the_account_from_the_census(
        self, temp_home
    ):
        """Barring a candidate must not make it cease to EXIST.

        Removing it from `oauth_candidates` fed eight consumers a list with a
        healthy account missing. `truly_exhausted` is the loudest: measured,
        peer 1 holding 15 points while the engine emitted AllExhaustedEvent —
        a macOS notification and a critical TUI row — because `all()` over the
        shortened list was vacuously true.

        DRIVES THE BARRED BRANCH, which is the part the previous tests missed.
        Peer 15 pts against active 10 pts does NOT satisfy `left >= active x
        2`, so the release does not fire and the bar is genuinely in effect.
        Both earlier tests used a 0% peer against a 97% active, where the
        release always fired — measured, disabling the filter outright left
        the whole suite green.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)

        assert h.tick_with_usage({
            "1": _usage(92, self._days_out(h, 500)),
            "2": _usage(10, self._days_out(h, 400)),
            "3": _usage(100, self._days_out(h, 300)),
        }) is TickOutcome.SWITCHED
        h.clock.advance(301.0)

        h.events.clear()
        h.tick_with_usage({
            "1": _usage(85, self._days_out(h, 400)),   # left; 15 pts, BARRED
            "2": _usage(90, self._days_out(h, 500)),   # active; 10 pts
            "3": _usage(100, self._days_out(h, 300)),  # genuinely spent
        })
        assert not any(isinstance(e, AllExhaustedEvent) for e in h.events), (
            f"events {[type(e).__name__ for e in h.events]} — account 1 holds "
            "15 points; barring it from the CHOICE must not erase it from the "
            "fleet"
        )

    def test_the_bar_lifts_when_it_would_leave_nothing(self, temp_home):
        """Identity has no release of its own, and the ratio cannot cover it.

        `lastSwitchFrom` is rewritten only by a successful switch — the one
        the bar prevents — so on two accounts it was permanent. The ratio
        release does not reach it either: `left >= active x 2` is unsatisfiable
        for any active headroom above 50, and consume-first fires exactly
        there. Measured before this: active 70 pts against a peer at 100 pts,
        20 ticks answering below-threshold, still locked after seven days.

        A bar that leaves the engine nothing to choose is a stall, not
        anti-flap.
        """
        h = EngineHarness(temp_home, strategy="consume-first")
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        assert h.tick_with_usage({
            "1": _usage7(95, 95, self._days_out(h, 500)),
            "2": _usage7(5, 5, self._days_out(h, 400)),
        }) is TickOutcome.SWITCHED
        h.clock.advance(301.0)

        outcomes = []
        for _ in range(20):
            outcomes.append(h.tick_with_usage({
                # left; weekly window resets SOONEST, which is what
                # consume-first ranks on
                "1": _usage7(0, 0, self._days_out(h, 10)),
                "2": _usage7(30, 30, self._days_out(h, 500)),   # active, 70 pts
            }))
            h.clock.advance(1801.0)

        assert TickOutcome.SWITCHED in outcomes, (
            f"20 ticks of {[o.name for o in outcomes]} — the only peer was "
            "barred with no way to lift it, so consume-first is off for good"
        )

    def test_the_bar_never_applies_to_an_escape(self, harness):
        """`at-limit` and `failover` skip every anti-flap gate by design.

        There we are escaping a dead or unreadable active, not optimising a
        return time — and the account we left may be the only place to go.
        Measured: dropping the trigger check left the full suite green, so
        nothing pinned it. The at-limit half is defended for the wrong reason
        (at-limit implies `active_headroom <= 0`, so the ratio release fires
        anyway); failover has no such accident, because an unreadable active
        gives `active_headroom is None` and the release cannot fire.

        Asserts on the BAR, not on a tick outcome: the ratio release makes the
        at-limit case pass either way, which is what hid this.
        """
        state = {"lastSwitchFrom": 1}
        # 15 pts against an active on 10: does NOT clear `left >= active x 2`,
        # so the ratio release cannot fire and only the trigger check can
        # answer. A peer far ahead would pass for the wrong reason.
        headroom = {"1": 15.0, "2": 10.0, "3": 1.0}
        for trigger in ("at-limit", "failover"):
            for active in (10.0, None):
                assert harness.engine._no_return_account(
                    trigger, state, headroom, active, ["1", "3"], harness.settings
                ) is None, (
                    f"trigger={trigger} active_headroom={active} barred the "
                    "account we left; an escape must reach every candidate"
                )
        # The control: the SAME state bars on a proactive tick.
        assert harness.engine._no_return_account(
            "proactive", state, headroom, 10.0, ["1", "3"], harness.settings
        ) == "1", "premise: these inputs are barred when the trigger allows it"

    @pytest.mark.parametrize(
        "landed,live,expect",
        [
            ("2", 2, "3"),   # the engine is still standing where it landed
            ("2", 3, "1"),   # the user moved 2 -> 3 by hand
            (2, 2, "3"),     # same pair, `lastSwitchTo` recorded as an int
            (2, 3, "1"),
            (None, 3, "3"),  # pre-upgrade record: no `lastSwitchTo` at all
        ],
    )
    def test_the_bar_only_holds_while_the_engine_is_where_it_landed(
        self, temp_home, landed, live, expect
    ):
        """A MANUAL switch away from the landing undoes the move the bar guards.

        The bar refuses to undo THIS ENGINE'S own last move. Once the user
        switches by hand the engine is no longer sitting where it put itself
        and that move is already undone, so barring where it came FROM
        protects nothing — it just withholds the fleet's best account.
        Reproduced: engine 1 -> 2, user 2 -> 3 by hand, account 1 still
        barred while it is the soonest account back, so the engine holds an
        active that returns an hour later until the at-limit escape.

        NO RELEASE LEG COVERS IT. `_no_return_account` returns the barred
        account as soon as `recovered` is False, before its ratio leg is
        read, and the leaves-nothing retry in `_rank` is gated on the same
        `recovered`. Account 1 is 1 point here against 8 at departure and its
        binding reset is the SAME absolute instant, so nothing on any axis
        says it improved — measured, and that is what makes the hold
        permanent rather than momentary.

        THROUGH `tick()`, NOT THE PREDICATE: `current` has to be threaded
        from the call site into `_no_return_account`, and this module has
        already shipped a bar whose unit was pinned while its wiring was not
        (see `test_the_bar_reaches_the_ranking_through_tick`). Measured here:
        dropping `kw["current"]` at the call site leaves a direct-call test
        of the gate entirely green.

        TYPES DIFFER ACROSS THE COMPARISON: `lastSwitchTo` is written from
        `_perform`'s `number: str` while `lastSwitchFrom` comes from
        `account_ref(number: int | None, ...)`. The `landed=2` / `live=2` row
        is the one a bare `==` fails — `2 != "2"` releases a bar that must
        hold — so the row asserting the HOLD is what pins the normalisation.

        ABSENT `lastSwitchTo` KEEPS THE BAR: a record written before the key
        existed cannot prove the engine moved away, and this module treats
        every other missing field the same conservative way. Letting absence
        disarm would silently drop the anti-flap bound for one upgrade cycle
        — the last row, whose engine stays put exactly like the on-landing
        rows.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        # Fixed absolute instants: account 1's reset must be the SAME instant
        # at departure and on the deciding tick, or the recovery leg reads a
        # reset creeping nearer as a genuine improvement and releases.
        back = {n: self._at(h, secs) for n, secs in
                (("1", 1800.0), ("2", 7200.0), ("3", 3600.0))}

        assert h.tick_with_usage({
            "1": _usage(92, back["1"]),    # 8 pts: the departure baseline
            "2": _usage(10, back["2"]),
            "3": _usage(92, back["3"]),
        }) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        state = h.engine._read_state()
        assert state["lastSwitchFrom"] == 1 and state["lastSwitchTo"] == "2", (
            f"premise: production writes an int `from` and a str `to` — got "
            f"{state.get('lastSwitchFrom')!r} / {state.get('lastSwitchTo')!r}"
        )
        h.engine._mutate_state(
            lambda st: st.pop("lastSwitchTo", None) if landed is None
            else st.__setitem__("lastSwitchTo", landed)
        )
        if live != 2:
            h.make_live("c@example.com", 3)   # the user switches BY HAND
        assert h.switcher.current_account_number() == str(live), (
            "premise: the live login is where this row says it is"
        )
        h.clock.advance(301.0)

        # Everything spent, so the ranking is on the recovery axis, where the
        # bar can change an answer at all. Account 1 is back soonest and is
        # WORSE than at departure (1 point against 8, same reset instant), so
        # no release leg can fire on its own.
        h.tick_with_usage({
            "1": _usage(99, back["1"]),   # barred; back in 30 min
            "2": _usage(99, back["2"]),   # back in 2h
            "3": _usage(99, back["3"]),   # back in 1h
        })
        # The LIVE login, not `activeAccountNumber`: a hand switch moves the
        # former and not the latter, which is the whole premise of this test.
        assert h.switcher.current_account_number() == expect, (
            f"lastSwitchTo={landed!r} live={live}: ended up on "
            f"{h.switcher.current_account_number()}, want {expect}. The bar "
            "must apply only while the engine is still standing on the "
            "account it switched to; a hand switch away undoes the move it "
            "guards"
        )

    def test_the_bar_actually_removes_the_account_from_the_ranking(
        self, harness
    ):
        """The bar has to BAR something, and nothing pinned that.

        Measured: disabling it outright — `if num == no_return` -> `if False`
        — left the whole suite green, this class included. Both sibling tests
        assert what happens when the bar is LIFTED (the census stays intact,
        the lockout ends), so neither notices when it never engages.

        Drives `_rank_candidates` directly. Through `tick()` the cooldown and
        the hysteresis gates decide these inputs first, so the same pair moves
        identically with the bar on and off — measured across 12 peer/active
        combinations, every one identical. The ranking is the only place the
        bar's effect is observable in isolation.
        """
        from claude_swap.settings import AutoSwitchSettings

        args = dict(
            trigger="proactive",
            consume_first=False,
            oauth_candidates=["1", "3"],
            usage={"1": _usage(40), "2": _usage(96), "3": _usage(99)},
            headroom={"1": 60.0, "2": 4.0, "3": 1.0},
            current="2",
            active_headroom=4.0,
            settings=AutoSwitchSettings(),
            now=harness.clock.now,
        )
        unbarred, _, _ = harness.engine._rank_candidates(no_return=None, **args)
        barred, _, _ = harness.engine._rank_candidates(no_return="1", **args)

        assert list(unbarred) == ["1"], (
            f"premise: account 1 holds 60 points against an active on 4 and "
            f"is the pick when nothing bars it — got {list(unbarred)}"
        )
        assert list(barred) == [], (
            f"the bar did not remove account 1 from the ranking: {list(barred)}"
        )

    def test_the_bar_lifts_when_the_only_alternative_cannot_be_chosen(
        self, temp_home
    ):
        """Existing is not the same as being an alternative.

        The leaves-nothing release asked whether any OTHER account exists. A
        third account that exists but can never qualify — at its limit, or with
        unreadable headroom — answered yes while offering the ranking nothing,
        so the release never fired and the n=2 stall simply moved to n>=3.

        Measured before this, one ordinary proactive move and no seeded state:
        30 ticks / 30h all BLOCKED with the active on 2 points and the barred
        peer on 3, while the same fleet with the bar cleared switches on the
        first tick.

        THIRD ACCOUNT AT ITS LIMIT on purpose: with a healthy third the bar is
        correct and the sibling tests cover it. The defect needs an alternative
        that the ranking loop would skip anyway.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)

        assert h.tick_with_usage({
            "1": _usage(92, self._days_out(h, 500)),
            "2": _usage(10, self._days_out(h, 400)),
            "3": _usage(100, self._days_out(h, 300)),
        }) is TickOutcome.SWITCHED
        h.clock.advance(301.0)

        outcomes = []
        for _ in range(30):
            outcomes.append(h.tick_with_usage({
                "1": _usage(97, self._days_out(h, 10)),    # barred, 3 pts
                "2": _usage(98, self._days_out(h, 500)),   # active, 2 pts
                "3": _usage(100, self._days_out(h, 300)),  # exists, spent
            }))
            h.clock.advance(3601.0)

        assert TickOutcome.SWITCHED in outcomes, (
            f"30 ticks of {[o.name for o in outcomes[:6]]}… — the only "
            "choosable peer was barred and the third account is at its limit, "
            "so the bar left the engine nothing"
        )

    def test_the_bar_lifts_for_an_alternative_the_ranking_would_reject(
        self, temp_home
    ):
        """Not-at-its-limit is not the same as rankable.

        The predicate above was `(headroom.get(n) or 0.0) > 0.0` — "has any
        points left". That is only the FIRST of the gates a candidate must
        clear: past the horizon it also needs `h >= active x HORIZON_HEADROOM_
        RATIO`, or the spent fallback's `h >= active` with a meaningfully
        sooner reset. A third account holding ONE point clears `> 0.0` and
        clears nothing else, so the release stayed shut and the n>=3 stall the
        release above was written for came straight back one point up.

        Measured, one ordinary proactive move and no seeded state: barred peer
        3.5 pts / back in 10h, active 2 pts / 500h out, third 1 pt — 30 ticks
        all BLOCKED. The control below is the same fleet with `lastSwitchFrom`
        popped and switches on the first tick, so the bar is the cause.

        This is the third time this release has been fixed one step short of
        the gate that actually decides (present -> not-at-limit -> rankable),
        which is why the fix is no longer a predicate that PREDICTS the
        ranking: `_tick_inner` now asks the ranking itself and re-ranks unbarred
        when the bar empties the list.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)

        assert h.tick_with_usage({
            "1": _usage(92, self._days_out(h, 500)),
            "2": _usage(10, self._days_out(h, 400)),
            "3": _usage(99, self._days_out(h, 300)),
        }) is TickOutcome.SWITCHED
        h.clock.advance(301.0)

        outcomes = []
        for _ in range(30):
            outcomes.append(h.tick_with_usage({
                "1": _usage(96.5, self._days_out(h, 10)),   # barred, 3.5 pts
                "2": _usage(98, self._days_out(h, 500)),    # active, 2 pts
                "3": _usage(99, self._days_out(h, 300)),    # 1 pt: > 0, unrankable
            }))
            h.clock.advance(3601.0)

        assert TickOutcome.SWITCHED in outcomes, (
            f"30 ticks of {[o.name for o in outcomes[:6]]}… — the third "
            "account holds one point, which passes `> 0.0` and no ranking "
            "gate, so the bar left the engine nothing"
        )

    def test_an_unreadable_barred_account_does_not_crash_the_tick(
        self, harness
    ):
        """`headroom.get(barred)` is None when that slot's usage is unreadable.

        The ratio release compares it against `active_headroom * RATIO`, so
        without the None check the tick raises `TypeError: '>=' not supported
        between instances of 'NoneType' and 'float'` — inside `_tick_inner`,
        on an ordinary proactive tick, whenever the account we just left has
        no readable usage. Measured: dropping `left_headroom is not None` left
        the whole suite green, so nothing pinned it.

        Asserts the CALL returns rather than the tick outcome: which account
        wins is the ranking's business, and a crash is the defect.
        """
        state = {"lastSwitchFrom": 1}
        headroom = {"2": 10.0, "3": 40.0}       # slot 1 unreadable — absent
        assert harness.engine._no_return_account(
            "proactive", state, headroom, 10.0, ["1", "3"], harness.settings
        ) == "1", (
            "an unreadable barred account must still bar — unknown headroom "
            "is not evidence it beats us"
        )

    def test_the_bar_lifts_for_a_peer_returning_inside_the_horizon(
        self, temp_home
    ):
        """The release had no condition on the RECOVERY axis at all.

        Its only release was `left >= active x HORIZON_HEADROOM_RATIO`, a pure
        headroom test — while `_recovery_is_useful` deliberately ranks by RESET
        when a candidate returns inside the horizon, and this module's own
        docstring names that case as the one the horizon exists to preserve:
        a weekly-bound active days out against a peer back in minutes.

        A barred peer in exactly that state was refused, because 4 points
        against an active on 3 misses `4 >= 3 x 2`. Measured on the predicate
        form: barred peer back in 1h, active 200h out — 10 ticks all BLOCKED.

        Asking the ranking covers it without a third predicate: barring the
        only account the reset axis would pick empties the list, so the retry
        opens. That is the point of not predicting — the release now follows
        every axis the ranking has, including ones added later.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        assert h.tick_with_usage({
            "1": _usage(92, self._days_out(h, 500)),
            "2": _usage(10, self._days_out(h, 400)),
        }) is TickOutcome.SWITCHED
        h.clock.advance(301.0)

        outcomes = []
        for _ in range(10):
            outcomes.append(h.tick_with_usage({
                "1": _usage(96, self._at(h, 3600)),      # barred, back in 1h
                "2": _usage(97, self._days_out(h, 200)),  # active, 200h out
            }))
            h.clock.advance(1801.0)

        assert TickOutcome.SWITCHED in outcomes, (
            f"{[o.name for o in outcomes]} — the bar refused the only peer "
            "returning inside the horizon, so the engine holds an account 200h "
            "out while a peer is back in one"
        )

    def test_the_same_fleet_moves_with_the_bar_cleared(self, temp_home):
        """The control for the test above: identical state, no bar.

        Without this, a stall could be the fleet's own numbers rather than the
        bar, and the assertion above would be measuring nothing. Same seeds,
        same usage, same clock — only `lastSwitchFrom` is popped.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)

        assert h.tick_with_usage({
            "1": _usage(92, self._days_out(h, 500)),
            "2": _usage(10, self._days_out(h, 400)),
            "3": _usage(99, self._days_out(h, 300)),
        }) is TickOutcome.SWITCHED
        h.clock.advance(301.0)
        h.engine._mutate_state(lambda st: st.pop("lastSwitchFrom", None))

        assert h.tick_with_usage({
            "1": _usage(96.5, self._days_out(h, 10)),
            "2": _usage(98, self._days_out(h, 500)),
            "3": _usage(99, self._days_out(h, 300)),
        }) is TickOutcome.SWITCHED, (
            "the control blocked too — the stall above is the fleet's numbers, "
            "not the bar, and that assertion is measuring nothing"
        )

    def test_the_bar_reaches_the_ranking_through_tick(self, temp_home):
        """The bar's production WIRING, which nothing pinned.

        `_no_return_account` is computed inside `_rank` and threaded into
        `_rank_candidates`. Measured: replacing that computation with
        `no_return = None` — the whole feature off in production — left the
        full suite green. The only test of the bar's effect drives
        `_rank_candidates` directly and passes `no_return` by hand, so the unit
        was pinned and the integration was not: any refactor that drops the
        kwarg reverts the anti-flap bound silently.

        Asserts a DIFFERENT DESTINATION, not a block: the leaves-nothing
        release is now answered by the ranking itself, so a bar that empties
        the list re-ranks unbarred and the engine moves anyway. A fleet where
        the bar blocks therefore proves nothing about the wiring — the only
        observable left is the engine landing somewhere else.

        ON THE RECOVERY AXIS, which is the only axis where the bar can change
        an answer at all. Past the horizon the release (`left >= active x
        RATIO` -> not barred) and the ranking gate (`h >= active x RATIO` ->
        qualifies) are the SAME inequality, so anything the bar could remove
        the loop had already dropped. Inside the horizon the ranking sorts by
        reset time instead, the two stop agreeing, and the bar bites. Both
        peers are back within the hour here, which is what puts the tick on
        that axis.

        ONE fleet, ticked twice: `temp_home` is a single home and a second
        `EngineHarness` over it inherits the first run's roster, so the control
        comes from popping `lastSwitchFrom`, not from a fresh box.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        assert h.tick_with_usage({
            "1": _usage(92, self._days_out(h, 500)),
            "2": _usage(10, self._days_out(h, 400)),
            "3": _usage(50, self._days_out(h, 300)),
        }) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        h.clock.advance(301.0)
        assert h.engine._read_state().get("lastSwitchFrom") is not None, (
            "premise: the move recorded what it left"
        )

        second = {
            "1": _usage(99, self._at(h, 1800)),      # left; back in 30 min
            "2": _usage(99, self._at(h, 7200)),      # active, spent, back in 2h
            "3": _usage(99, self._at(h, 3600)),      # back in 1h
        }
        assert h.tick_with_usage(second) is TickOutcome.SWITCHED
        assert h.active_number() == 3, (
            "the bar never reached the ranking through tick() — the engine "
            "went back to the account it had just left, which returns sooner "
            "than the peer it should have taken"
        )

        # Control: same numbers, the bar removed — the soonest return wins.
        # The clock advances past the post-switch cooldown, and `second`'s
        # resets are relative to the ORIGINAL now, so both peers are still
        # ahead of the active by the same margins.
        h.make_live("b@example.com", 2)
        h.clock.advance(301.0)
        h.engine._mutate_state(lambda st: st.pop("lastSwitchFrom", None))
        assert h.tick_with_usage(second) is TickOutcome.SWITCHED
        assert h.active_number() == 1, (
            "premise: unbarred, the account we left returns soonest and IS the "
            "pick — without this the assertion above would pass on a fleet "
            "where 3 wins for its own reasons"
        )

    def test_the_release_needs_the_barred_account_to_have_improved(
        self, temp_home
    ):
        """An empty barred ranking is a reason to ASK, not a reason to release.

        On two accounts the barred ranking is ALWAYS empty — barring the only
        candidate necessarily empties the list — so a release keyed on
        emptiness alone is a no-op at n=2, which is the fleet size the flap was
        reported on. Measured on the emptiness-only release, sweeping active x
        barred headroom x both reset shapes through `_rank_candidates(
        no_return="1", oauth_candidates=["1"])`:

            n=2 barred-rank EMPTY=320 NONEMPTY=0

        So the retry fired every time and the bar never applied. The cited flap
        reproduced unchanged: pcts 92/92, resets 500h/400h, 60 ticks gave
        `[1, 2, 1, 2]` with the bar ON and `[1, 2, 1, 2]` with `lastSwitchFrom`
        popped every tick — identical, and worse than base's single move.

        WHAT SEPARATES THE TWO STATES is not the ranking, which only sees the
        present. It is whether the barred account is a different proposition
        from the one we left. At each leg of that walk it was not:

            t8   1->2   left 1 holding 4.0 pts, 500h out
            t20  2->1   account 1 holds 4.0 pts, 500h out   <- nothing changed
            t22  1->2   account 2 holds 2.0 pts, 400h out   <- nothing changed

        Every return won because the ACTIVE burned down, never because the
        target recovered. That is the flap, exactly.

        Both legs of the test below are the flap shape: the barred account is
        no better than we left it on either axis. The bar must hold even
        though the ranking is empty and the tick therefore does nothing.

        WALKED PAST THE OLD BOUNDARY: the pre-fix dominance leg was a bare
        `h > active x RATIO`, which held only up to and including ratio
        exactly 2.00 (`active=2.0` pts) and opened on the very next cell
        (`active=1.8`) — measured, on this fleet. The fixed leg adds a flat
        `+SPENT_HEADROOM_PCT` on top of the ratio, so the boundary against
        this same 4.0-pt frozen
        peer moves from `active=2.0` to `active=0.5` — the walk below covers
        every cell in between, well past where the bare ratio opened.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        assert h.tick_with_usage({
            "1": _usage(96, self._days_out(h, 500)),   # 4 pts
            "2": _usage(92, self._days_out(h, 400)),   # 8 pts
        }) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        h.clock.advance(3612.0)

        outcomes = []
        # 98 (2.0 pts, the old exact boundary) through 99.5 (0.5 pts, the new
        # boundary's last holding cell) — well past where the bare ratio
        # opened, at 1.8 pts.
        for active_pct in (98.0, 98.2, 98.4, 99.0, 99.5):
            outcomes.append(h.tick_with_usage({
                # unchanged since we left it: same headroom, same reset
                "1": _usage(96, self._days_out(h, 500)),
                "2": _usage(active_pct, self._days_out(h, 400)),   # active, burnt down
            }))
            h.clock.advance(301.0)

        assert TickOutcome.SWITCHED not in outcomes, (
            f"{[o.name for o in outcomes]} — the engine went back to an "
            "account that is exactly as we left it. The ranking flipped "
            "because the active burned, not because the target recovered; "
            "that is the flap this bar exists for."
        )

    def test_the_release_fires_on_this_same_fleet_once_the_peer_actually_improves(
        self, temp_home
    ):
        """The release partner for the hold above: SAME fleet, PEER moves.

        The sibling test proves the bar holds no matter how far the active
        burns while the peer is frozen. Without this partner, an
        over-conservative predicate that never releases at all — the
        permanent 2-account lockout this branch has already fixed twice —
        would also pass that test, since "never SWITCHED" is satisfied
        trivially by "never releases anything, ever". This uses the exact
        same departure (1 at 4.0 pts / 500h, 2 at 8.0 pts / 400h) and then
        lets account 1 recover to full quota while the active sits at the
        SAME 98% the sibling test holds at — the only variable that changes
        is the peer.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        assert h.tick_with_usage({
            "1": _usage(96, self._days_out(h, 500)),   # 4 pts
            "2": _usage(92, self._days_out(h, 400)),   # 8 pts
        }) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        h.clock.advance(3612.0)

        outcomes = []
        for _ in range(10):
            outcomes.append(h.tick_with_usage({
                "1": _usage(0, self._days_out(h, 500)),    # RECOVERED: reset to full
                "2": _usage(98, self._days_out(h, 400)),   # active, same 98% as the hold
            }))
            h.clock.advance(301.0)

        assert TickOutcome.SWITCHED in outcomes, (
            f"{[o.name for o in outcomes]} — account 1 reset to full quota, "
            "the peer's own state, and the engine never returned; a bar that "
            "never releases at any active burn is the permanent lockout, not "
            "anti-flap"
        )

    def test_a_departure_at_full_quota_is_immediately_eligible(self, temp_home):
        """`left_headroom == 100.0` must not be a permanent lockout.

        `h >= left_headroom + SPENT_HEADROOM_PCT` is `h >= 103.0` when the
        departure was recorded at a full 100.0 points — unsatisfiable forever,
        because `oauth.account_headroom` caps `h` at 100.0
        (`100 - max(pct)`, and pct cannot go negative). consume-first departs
        BELOW the threshold, so this is the routine case, not a corner one: a
        fresh/full account handed off to a sooner-resetting peer records
        exactly this.

        The account below holds the SAME 100.0 points at every check (never
        spent anything) and the SAME resets_at (no recovery-axis movement
        either) — the only way this switches is if a departure at the cap is
        treated as needing no recovery on the headroom axis.
        """
        h = EngineHarness(temp_home, strategy="consume-first", threshold=90.0)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        assert h.tick_with_usage({
            "1": _usage7(0.0, 0.0, self._days_out(h, 500)),
            "2": _usage7(0.0, 0.0, self._days_out(h, 100)),
        }) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert h.engine._read_state().get("leftHeadroom") == 100.0, (
            "premise: consume-first recorded a full-quota departure"
        )
        h.clock.advance(301.0)

        outcomes = []
        for _ in range(10):
            outcomes.append(h.tick_with_usage({
                # unchanged since departure on BOTH axes
                "1": _usage7(0.0, 0.0, self._days_out(h, 500)),
                "2": _usage7(90.0, 0.0, self._days_out(h, 100)),
            }))
            h.clock.advance(301.0)

        assert TickOutcome.SWITCHED in outcomes, (
            f"{[o.name for o in outcomes]} — account 1 never dropped below a "
            "full 100.0 points and is the only peer; refusing it is the "
            "unsatisfiable-above-97 lockout, not anti-flap"
        )

    def test_the_clamp_stays_load_bearing_when_dominance_does_not_fire(
        self, harness
    ):
        """Dominance must not shadow the `min(..., 100.0)` clamp — a new
        leg silently disarming an existing guard's killer, a shape this
        module has hit before.

        The sibling test above (`test_a_departure_at_full_quota_is_
        immediately_eligible`) uses active=10, where dominance ALSO fires
        (`100 > 10*2+3`), so mutating away the clamp there is invisible: the
        dominance leg answers True regardless of the clamp. Isolate the
        clamp with an active_headroom chosen so dominance is FALSE
        (`100 > 60*2+3=123` is false) while the clamp (`100 >= min(98+3,
        100)`) is still True — exactly the shape measured (left=98 /
        peer=100 / active=60) as flipping between clamp on/off.
        """
        state = {"lastSwitchFrom": "1", "leftHeadroom": 98.0, "leftRecoveryAt": None}
        recovered = harness.engine._left_account_recovered(
            state,
            {"1": _usage(0)},
            {"1": 100.0},
            60.0,
            harness.settings,
            harness.clock(),
        )
        assert recovered is True, (
            "the clamp must still release a departure recorded at a near-full "
            "leftHeadroom even where dominance over the active does not fire"
        )
        # Control: without the clamp (`h >= left_headroom + SPENT_HEADROOM_PCT`
        # unclamped -> `h >= 101.0`), 100.0 fails and this would be False —
        # confirms the clamp, not some other leg, is what makes it True.
        assert not (100.0 >= 98.0 + SPENT_HEADROOM_PCT), (
            "premise: the unclamped threshold is unsatisfiable at h=100.0"
        )

    def test_the_dominance_leg_does_not_silently_read_an_unreadable_active_as_no_dominance(
        self, harness
    ):
        """`active_headroom is None` must not collapse onto the same
        answer as "the peer genuinely does not dominate".

        The consume-first two-phase commit reassigns `active_headroom` from
        an escalated refetch WITHOUT re-classifying the trigger
        (`autoswitch.py:1152-1168`) -- so a phase-2 refetch that cannot read
        the active reaches this predicate with `trigger in ("proactive",
        "consume-first")` (in scope for the bar) and `active_headroom is
        None`. Before the fix, the dominance leg's own `active_headroom is
        not None` guard silently turned "cannot compare" into "does not
        dominate", identical to a peer that genuinely fails the ratio test.

        Same peer (frozen at 40 pts, well past the `+SPENT_HEADROOM_PCT`
        margin at any active this small) with only the active's readability
        changed:

            active_headroom=2.0   (readable)   -> True
            active_headroom=None  (unreadable) -> must ALSO be True

        Measured pre-fix: readable gave True (`40 > 2*2+3`), unreadable gave
        False -- same peer, same reality, opposite answers, purely from
        losing the ability to read a THIRD account.
        """
        state = {"lastSwitchFrom": "2", "leftHeadroom": 40.0, "leftRecoveryAt": None}
        usage = {"2": _usage(60.0)}  # peer frozen at 40 pts headroom
        readable = harness.engine._left_account_recovered(
            state, usage, {"2": 40.0}, 2.0, harness.settings, harness.clock(), "1",
        )
        unreadable = harness.engine._left_account_recovered(
            state, usage, {"2": 40.0}, None, harness.settings, harness.clock(), "1",
        )
        assert readable is True, "premise: a readable, dominant active releases"
        assert unreadable is True, (
            f"readable={readable} unreadable={unreadable} -- the SAME peer, "
            "unchanged, must not flip to HOLD purely because the active "
            "became unreadable; that silently scores 'cannot read' the same "
            "as 'does not dominate'"
        )

    def test_no_return_account_does_not_re_bar_an_already_recovered_peer_when_the_active_is_unreadable(
        self, harness
    ):
        """Measured directly against `_no_return_account`: with `recovered`
        already established True, the function's OWN ratio leg has the same
        `active_headroom is None` ambiguity as the predicate that feeds it --
        an unreadable active must not re-impose the bar on a peer already
        judged recovered.

        `recovered=True` is fixed on both calls (this isolates
        `_no_return_account`'s own leg from the fix already applied to
        `_left_account_recovered`). Same barred peer at 40 pts, dominating a
        2-pt active by 20x -- readable releases (`None`); measured pre-fix
        that unreadable stayed barred (`'2'`) for the identical peer, purely
        because the second, redundant ratio check inside `_no_return_account`
        could not confirm dominance without a readable active.
        """
        state = {"lastSwitchFrom": "2", "leftHeadroom": 40.0, "leftRecoveryAt": None}
        headroom = {"2": 40.0}
        for trigger in ("proactive", "consume-first"):
            readable = harness.engine._no_return_account(
                trigger, state, headroom, 2.0, recovered=True, settings=harness.settings
            )
            unreadable = harness.engine._no_return_account(
                trigger, state, headroom, None, recovered=True, settings=harness.settings
            )
            assert readable is None, f"premise: {trigger} releases when readable"
            assert unreadable is None, (
                f"trigger={trigger} readable={readable!r} "
                f"unreadable={unreadable!r} -- the same already-recovered peer "
                "must not be re-barred purely because the active became "
                "unreadable"
            )

    def test_the_all_spent_recovery_leg_carries_its_own_hysteresis(
        self, temp_home
    ):
        """The failover recovery leg needs a margin too.

        Without `RECOVERY_HYSTERESIS_S`, any reset that drifts a second
        nearer than the active's reads as "the barred peer recovered" --
        the same drift-is-not-recovery hole the ordinary path's recovery
        leg already guards against (`test_a_reset_that_crept_nearer_is_not_
        a_recovery`), but on the failover leg. Both accounts sit inside the
        all-spent band (peer 5 pts, active 2 pts, threshold
        90 -> floor 10) so the landing leg cannot fire and only the
        recovery leg decides; the peer's reset is 60s sooner than the
        active's, well inside the 300s margin, so this must NOT release.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        state = {"lastSwitchFrom": "1", "leftHeadroom": None, "leftRecoveryAt": None}
        usage = {
            "1": _usage(95.0, self._at(h, 3600.0)),        # 5 pts, back in 1h
            "2": _usage(98.0, self._at(h, 3660.0)),        # 2 pts, back in 1h+60s
        }
        recovered = h.engine._left_account_recovered(
            state, usage, {"1": 5.0}, 2.0, h.settings, h.clock(), "2",
        )
        assert recovered is False, (
            "the peer's reset is only 60s sooner than the active's, well "
            "inside RECOVERY_HYSTERESIS_S (300s) -- without the margin any "
            "drift in resets_at releases the all-spent failover hold"
        )

    def test_the_dominance_fallback_does_not_fire_below_its_own_floor(
        self, harness
    ):
        """The unreadable-active fallback still has a floor -- it is not an
        unconditional release once the active goes unreadable.

        Ordinary-path snapshot (`leftHeadroom` a real baseline), active
        unreadable, and the peer currently BELOW the landing floor
        (`h=5 < 100-90=10`). None of the other legs can fire either (peer
        far under `left_headroom + SPENT_HEADROOM_PCT`, and no reset info
        at all so the recovery leg's `inf < inf - 300` is false), so a
        correct predicate holds.
        """
        state = {"lastSwitchFrom": "2", "leftHeadroom": 40.0, "leftRecoveryAt": None}
        usage = {"2": _usage(95.0)}  # 5 pts, no resets_at at all
        recovered = harness.engine._left_account_recovered(
            state, usage, {"2": 5.0}, None, harness.settings, harness.clock(), "1",
        )
        assert recovered is False, (
            "the peer is unreadable-active-fallback-eligible in shape only -- "
            "at 5 pts it is BELOW the landing floor (10), so the fallback "
            "must not release it just because the active went unreadable"
        )

    def test_no_return_accounts_unreadable_active_fallback_has_a_floor_too(
        self, harness
    ):
        """`_no_return_account`'s own unreadable-active fallback leg needs
        the same floor as the predicate that feeds it -- not an
        unconditional release once the active is unreadable.

        `recovered=True` fixed (isolates this leg). Barred peer at 5 pts,
        active unreadable -- 5 is BELOW the landing floor (10 at the
        default threshold), so the bar must still apply.
        """
        state = {"lastSwitchFrom": "2"}
        headroom = {"2": 5.0}
        no_return = harness.engine._no_return_account(
            "proactive", state, headroom, None, recovered=True,
            settings=harness.settings,
        )
        assert no_return == "2", (
            "the barred peer at 5 pts is below the landing floor (10); an "
            "unreadable active must not unconditionally release it"
        )

    def test_a_reset_that_crept_nearer_is_not_a_recovery(self, temp_home):
        """The recovery leg carries `RECOVERY_HYSTERESIS_S`, and it must.

        Without a margin (`< was - 0.0`) any reset that moved a second nearer
        counts as the barred account "recovering", and a `resets_at` that
        drifts — a refetch landing a slightly different estimate, or simply a
        nearer window starting to bind — hands the flap a release for free.
        That is the same shape as the ratio gate before it was gated: a
        threshold burn crosses on its own.

        Measured with the margin removed: the walk below returns to account 1
        because its binding reset reads 60s nearer than the value recorded at
        departure, while its headroom is unchanged.

        `RECOVERY_HYSTERESIS_S` is the margin the recovery AXIS already ranks
        by one gate later, so the release and the ranking agree about what
        "meaningfully sooner" means rather than being two numbers to reason
        about separately.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        depart = self._at(h, 500 * 3600)
        assert h.tick_with_usage({
            "1": _usage(96, depart),                    # 4 pts
            "2": _usage(92, self._days_out(h, 400)),    # 8 pts
        }) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        h.clock.advance(3612.0)

        outcomes = []
        for _ in range(10):
            outcomes.append(h.tick_with_usage({
                # same headroom as at departure; the reset crept 60s nearer,
                # which is well inside RECOVERY_HYSTERESIS_S
                "1": _usage(96, self._at(h, 500 * 3600 - 3612 - 60)),
                "2": _usage(98, self._days_out(h, 400)),
            }))
            h.clock.advance(301.0)

        assert TickOutcome.SWITCHED not in outcomes, (
            f"{[o.name for o in outcomes]} — a reset one minute nearer is not "
            "the barred account recovering; without the margin any drift in "
            "`resets_at` releases the bar"
        )

    def test_an_unschedulable_account_that_gained_a_reset_has_recovered(
        self, temp_home
    ):
        """`inf` is the right departure value for an unknown reset, not zero.

        `_binding_recovery_ts` returns `inf` for a binding window with no
        usable `resets_at` — an account nobody can schedule around — and
        `_perform` stores that as JSON `null`. Reading it back as `0.0` makes
        the recovery leg unsatisfiable, because no real timestamp is below
        `0 - RECOVERY_HYSTERESIS_S`, so an account that gained a reset while we
        were away is refused forever on that axis.

        That IS an improvement, and it is one the headroom leg cannot see: the
        account below holds the same 4 points it had at departure, so only the
        reset changed. Measured with the default flipped to `0.0`: 10 ticks
        BLOCKED with the peer back in an hour.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        assert h.tick_with_usage({
            "1": _usage(96),                            # 4 pts, NO reset known
            "2": _usage(92, self._days_out(h, 400)),
        }) is TickOutcome.SWITCHED
        assert h.engine._read_state().get("leftRecoveryAt") is None, (
            "premise: the departure reset was unknown and stored as null"
        )
        h.clock.advance(3612.0)

        outcomes = []
        for _ in range(10):
            outcomes.append(h.tick_with_usage({
                # SAME 4 points; only the reset is now known, and it is near
                "1": _usage(96, self._at(h, 3600)),
                "2": _usage(98, self._days_out(h, 400)),
            }))
            h.clock.advance(301.0)

        assert TickOutcome.SWITCHED in outcomes, (
            f"{[o.name for o in outcomes]} — the barred account went from "
            "unschedulable to back-in-an-hour, which the headroom leg cannot "
            "see; reading the stored null as 0.0 makes that unreachable"
        )

    def test_a_switch_that_recorded_no_snapshot_still_releases(
        self, temp_home
    ):
        """No departure snapshot means release, and that direction is chosen.

        State written before `leftHeadroom`/`leftRecoveryAt` existed — an
        upgrade in place, with the file persisted across restarts — names a
        barred account and carries no evidence about it either way. The two
        failure modes are not symmetric: barring on absent evidence is the
        permanent proactive lockout this branch has already fixed twice, and it
        survives a restart and a week of wall clock, while releasing costs at
        most one extra move that the next switch then records properly.

        Measured with the default flipped to `return False`: the shape below
        answers BLOCKED for 20 ticks with a peer at full quota.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        # A pre-upgrade record: the bar is named, the snapshot is not there.
        h.engine._mutate_state(lambda st: st.update({"lastSwitchFrom": "2"}))
        h.engine._mutate_state(lambda st: st.pop("leftHeadroom", None))
        h.engine._mutate_state(lambda st: st.pop("leftRecoveryAt", None))
        assert "leftHeadroom" not in h.engine._read_state(), (
            "premise: the state carries no departure snapshot"
        )

        outcomes = []
        for _ in range(20):
            outcomes.append(h.tick_with_usage({
                "1": _usage(97, self._days_out(h, 500)),   # active, 3 pts
                "2": _usage(0, self._days_out(h, 400)),    # barred, FULL quota
            }))
            h.clock.advance(1801.0)

        assert TickOutcome.SWITCHED in outcomes, (
            f"20 ticks of {[o.name for o in outcomes[:6]]}… — state written "
            "before the snapshot field existed barred the only peer forever, "
            "which is the persisted lockout, not anti-flap"
        )

    def test_absence_of_a_snapshot_releases_even_a_poor_or_unreadable_peer(
        self, harness
    ):
        """The pre-upgrade absence guard must fire before the null check.

        The sibling test above only drives absence with a peer at FULL quota
        — `(headroom.get(barred) or 0.0) >= 100.0 - SPENT_HEADROOM_PCT` also
        happens to return `True` for that shape, so a suite with only that
        case cannot tell "the absence guard ran" from "the near-full floor
        happened to agree with it". Genuinely absent keys carry NO evidence
        either way (`_perform` never ran to record any), which is a different
        state from a failover's `(None, None)` — real evidence the departure
        was unmeasurable — and the two must not collapse onto the same
        answer merely because they share a code path once the leading `if
        "leftHeadroom" not in state: return True` guard is gone.

        Drives the two shapes that DO tell them apart: the barred account
        POOR (well under the near-full floor) and UNREADABLE (`headroom` is
        `None`, which the null-check branch maps to `0.0` via `or 0.0` and
        so also fails the floor). Absence must still release both.
        """
        state = {"lastSwitchFrom": "2"}  # pre-upgrade: keys genuinely absent

        assert harness.engine._left_account_recovered(
            state, {"2": _usage(96)}, {"2": 4.0}, 2.0, harness.settings, harness.clock()
        ) is True, (
            "a pre-upgrade record (no snapshot) must release even when the "
            "barred account is currently poor (4 pts) — absence of evidence "
            "is not the same state as a measured-unmeasurable failover"
        )
        assert harness.engine._left_account_recovered(
            state, {"2": None}, {"2": None}, 2.0, harness.settings, harness.clock()
        ) is True, (
            "a pre-upgrade record (no snapshot) must release even when the "
            "barred account is currently unreadable — absence of evidence "
            "releases regardless of what can be measured right now"
        )

    def test_a_failover_departure_does_not_disarm_the_bar(self, temp_home):
        """`(None, None)` from a failover must not read the same as `absent`.

        `_perform` writes `leftHeadroom`/`leftRecoveryAt` unconditionally on
        every trigger, including `failover`, where `active_headroom` is None
        (that is the definition of failover) and the recorded recovery is
        `inf` -> stored as `null`. The resulting state —
        `{"leftHeadroom": null, "leftRecoveryAt": null}`, KEYS PRESENT — is
        byte-identical over JSON to a pre-upgrade record where the keys were
        never written, and the old code read both with `state.get(...)`,
        which cannot tell presence-with-null from absence.

        Reached with a real failover (active usage unreadable for
        `unhealthy_ticks` ticks), then the classic flap shape: the barred
        account frozen at 4 pts, the new active burning from 4 pts down past
        the exact boundary (98.2%) a bare dominance leg opens at —
        `4.0 > 1.8 x 2` — walked further still, to a bare sliver (99.9%,
        0.1 pts). Measured on this exact fleet, the bare leg switched back
        to the frozen peer at 98.2%. This leaves the ORDINARY-path shape
        (`test_the_release_needs_the_barred_account_to_have_improved`) as
        the sibling proving the general
        dominance leg's own margin separately; this one is failover-only,
        which never reads `leftHeadroom`/`leftRecoveryAt` at all, so it also
        discriminates a relational fix from an absolute one: mutate the
        failover leg to a bare `h >= 4.0` (the peer's own constant value) and
        every tick below flips to SWITCHED, because that mutant no longer
        reads the active at all.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        frozen1 = self._days_out(h, 500)
        outcome = None
        for _ in range(3):  # unhealthy_ticks default is 3
            outcome = h.tick_with_usage({
                "1": None,                              # unreadable -> failover
                "2": _usage(4, self._days_out(h, 400)),
            })
            h.clock.advance(60.0)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        state = h.engine._read_state()
        assert "leftHeadroom" in state, (
            "premise: _perform writes the keys unconditionally even on failover"
        )
        assert state.get("leftHeadroom") is None
        assert state.get("leftRecoveryAt") is None

        h.clock.advance(301.0)
        outcomes = []
        # 98.0 (2.0 pts, the old boundary), 98.2 (1.8 pts, where the bare
        # leg opened), then further still down to a bare sliver — the peer
        # never moves, so nothing on this walk is evidence it improved.
        for active_pct in (98.0, 98.2, 98.4, 99.0, 99.5, 99.9):
            outcomes.append(h.tick_with_usage({
                "1": _usage(96, frozen1),                     # frozen, 4 pts
                "2": _usage(active_pct, self._days_out(h, 400)),
            }))
            h.clock.advance(301.0)

        assert TickOutcome.SWITCHED not in outcomes, (
            f"{[o.name for o in outcomes]} — a failover departure was treated "
            "as 'no evidence, release', undoing the failover once the active "
            "burned far enough, however far — the peer never changed"
        )

    def test_a_failover_departure_still_unreadable_does_not_crash_or_release(
        self, temp_home
    ):
        """The barred peer staying unreadable after a failover must hold cleanly.

        `_left_account_recovered`'s `(None, None)` branch reads the barred
        account's CURRENT headroom to tell "still unmeasurable" from
        "measurable again". `headroom.get(barred)` is `None` when the peer is
        still unreadable, and a comparison against that (`None >= 97.0`)
        raises `TypeError` in Python 3 rather than silently doing the wrong
        thing — so a guard that drops the `is not None` check is not a subtle
        release-too-early bug, it is a crash on every tick this shape
        produces. Guard against both: no exception, and no release on
        no-evidence-either-way.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        outcome = None
        for _ in range(3):  # unhealthy_ticks default is 3
            outcome = h.tick_with_usage({
                "1": None,                              # unreadable -> failover
                "2": _usage(4, self._days_out(h, 400)),
            })
            h.clock.advance(60.0)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

        h.clock.advance(301.0)
        outcomes = []
        for _ in range(10):
            outcomes.append(h.tick_with_usage({
                "1": None,                                    # still unreadable
                "2": _usage(98, self._days_out(h, 400)),       # active, burnt down
            }))
            h.clock.advance(301.0)

        assert TickOutcome.ERROR not in outcomes, (
            f"{[o.name for o in outcomes]} — comparing the barred peer's "
            "unreadable (`None`) headroom against the release threshold must "
            "not raise; a bare `h >= ...` with no `is not None` guard does"
        )
        assert TickOutcome.SWITCHED not in outcomes, (
            f"{[o.name for o in outcomes]} — the barred peer never became "
            "readable; that is 'still unmeasurable', not a recovery"
        )

    def test_a_failover_departure_releases_once_the_peer_is_readable_again(
        self, temp_home
    ):
        """`(None, None)` must not be a PERMANENT lockout once the peer heals.

        The sibling test above holds the bar while the failed-over peer is
        STILL unreadable or still poor — correct, and the flap 0b369e0 fixed.
        But `_left_account_recovered`'s original fix (`return False`
        unconditionally on `(None, None)`) held it forever: nothing in that
        branch ever looked at the peer's CURRENT state, so a peer that went
        unreadable -> readable at full quota with a near reset could never
        release the proactive/consume-first bar. On a 2-account fleet there
        is no third account to switch to, so that is a permanent proactive
        lockout, not anti-flap.

        Same failover setup as the sibling test, but this time account 1
        comes back READABLE at full quota with a near reset while the active
        burns below the threshold — a real change of state, not a flap.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        outcome = None
        for _ in range(3):  # unhealthy_ticks default is 3
            outcome = h.tick_with_usage({
                "1": None,                              # unreadable -> failover
                "2": _usage(4, self._days_out(h, 400)),
            })
            h.clock.advance(60.0)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        state = h.engine._read_state()
        assert state.get("leftHeadroom") is None and state.get(
            "leftRecoveryAt"
        ) is None, "premise: a failover snapshot, keys present, values null"

        h.clock.advance(301.0)
        outcomes = []
        for _ in range(10):
            outcomes.append(h.tick_with_usage({
                "1": _usage(0, self._days_out(h, 10)),    # READABLE, full, near reset
                "2": _usage(95, self._days_out(h, 400)),  # active below threshold
            }))
            h.clock.advance(301.0)

        assert TickOutcome.SWITCHED in outcomes, (
            f"{[o.name for o in outcomes]} — peer 1 went unreadable -> "
            "readable at full quota with a near reset while the active fell "
            "below the threshold. The engine stayed put: a permanent "
            "proactive lockout, not the bounded hold this predicate should "
            "produce."
        )

    def test_a_failover_departure_releases_a_healthy_but_not_near_full_peer(
        self, temp_home
    ):
        """`(None, None)` must release a HEALTHY peer, not just a near-full one.

        `test_a_failover_departure_releases_once_the_peer_is_readable_again`
        only proves the bar is not PERMANENT — it drives the peer to a full
        100.0, which also clears a fixed near-100 floor. The original bug
        here was an absolute `>= 97.0` floor, unreachable for any peer that
        had spent more than 3% of its weekly window.

        THIS IS DELIBERATELY NOT RELATIONAL TO THE ACTIVE: making this
        branch dominance-vs-active is exactly what broke
        `test_a_failover_departure_does_not_disarm_the_bar`
        — a `(None, None)` snapshot has no recorded baseline for either
        headroom or recovery, so there is nothing to diff the ACTIVE's burn
        away from; any leg that reads the active here reproduces the flap the
        moment the active burns far enough, however far. What this branch
        uses instead is `h > 100 - settings.threshold`: the same "would the
        ranking accept this as a landing spot" test every candidate already
        passes (`:1617`), so the floor is the user's OWN policy rather than a
        hardcoded constant, and it moves when they change it. It genuinely
        cannot tell "the peer just recovered" from "the peer was always this
        good" — there is nothing recorded to tell them apart on a failover
        departure — so both land on RELEASE, which is the documented,
        measurement-backed choice (see `_left_account_recovered`'s docstring)
        for a case R-A and R-B cannot be separated in.

        Reached with a real failover, same setup as the sibling tests, then
        the peer comes back READABLE at 70 points (well over the default
        threshold-derived floor of 10, well over the 4-point flap the bar
        must still catch) while the active burns to 2.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        outcome = None
        for _ in range(3):  # unhealthy_ticks default is 3
            outcome = h.tick_with_usage({
                "1": None,                              # unreadable -> failover
                "2": _usage(4, self._days_out(h, 400)),
            })
            h.clock.advance(60.0)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        state = h.engine._read_state()
        assert state.get("leftHeadroom") is None and state.get(
            "leftRecoveryAt"
        ) is None, "premise: a failover snapshot, keys present, values null"

        h.clock.advance(301.0)
        outcomes = []
        for _ in range(10):
            outcomes.append(h.tick_with_usage({
                "1": _usage(30, self._days_out(h, 10)),   # READABLE, 70 pts
                "2": _usage(98, self._days_out(h, 400)),  # active burnt to 2 pts
            }))
            h.clock.advance(301.0)

        assert TickOutcome.SWITCHED in outcomes, (
            f"{[o.name for o in outcomes]} — peer 1 is READABLE at 70 points, "
            "35x the active's 2 points, and the engine never returned. The "
            "release floor is absolute (h >= 97), so any peer past 3% of its "
            "weekly window is held until the active hits its own limit."
        )

    def test_a_failover_departure_releases_when_the_peer_resets_first_in_the_all_spent_regime(
        self, temp_home
    ):
        """The failover floor is the exact complement of all-spent, so a
        peer that resets first can never release the bar.

        `_every_account_above_threshold` is True exactly when every account,
        active included, sits at or under `100 - threshold`. The failover
        release floor at `_left_account_recovered` demands the barred peer
        sit STRICTLY ABOVE `100 - threshold` -- the exact complement of the
        same quantity. So whenever the fleet is all-spent, the floor cannot
        be cleared by any measured headroom, independent of how soon the
        peer's own binding window resets -- which is exactly the axis
        `TestAllSpentGoesToTheSoonestReset` says should decide once headroom
        stops being informative.

        Same failover setup as the sibling tests above, but both accounts
        now sit inside the all-spent band (peer 2.5 pts, active 2.0 pts --
        both under the default floor of 10) and the peer's binding reset is
        5 minutes away against the active's 400 hours out. See the sibling
        test below for the snapshot-stripped control proving this exact
        fleet is choosable, so a stall here is the floor, not the ranking.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        outcome = None
        for _ in range(3):  # unhealthy_ticks default is 3
            outcome = h.tick_with_usage({
                "1": None,                              # unreadable -> failover
                "2": _usage(4, self._at(h, 400 * 3600)),
            })
            h.clock.advance(60.0)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        state = h.engine._read_state()
        assert state.get("leftHeadroom") is None and state.get(
            "leftRecoveryAt"
        ) is None, "premise: a failover snapshot, keys present, values null"

        h.clock.advance(301.0)
        outcomes = []
        for _ in range(8):
            outcomes.append(h.tick_with_usage({
                "1": _usage(97.5, self._at(h, 300.0)),      # 2.5 pts, 5 min out
                "2": _usage(98.0, self._days_out(h, 400)),  # 2.0 pts, 400h out
            }))
            h.clock.advance(301.0)

        assert TickOutcome.SWITCHED in outcomes, (
            f"{[o.name for o in outcomes]} -- peer 1 sits inside the "
            "all-spent band at 2.5 pts and resets in 5 minutes against the "
            "active's 2.0 pts / 400h out; the floor `h > 100 - threshold` is "
            "the complement of all-spent so it can never release here "
            "however soon the peer returns"
        )

    def test_the_recovery_leg_requires_the_actives_reset_to_be_known_not_merely_absent(
        self, temp_home
    ):
        """`_binding_recovery_ts` returns `inf` for FIVE states, only two
        of which mean "never" (unreadable, token-expired
        sentinel) -- unknown resets_at and a stale/past resets_at both also
        return `inf` but mean "we do not know", not "never". The old
        predicate `peer < active - HYST` treats all of them alike, so an
        active whose `resets_at` is simply unreported reads as WORSE than a
        peer that is finite but arbitrarily far out (400h), and the bar
        releases onto it on no evidence at all.

        Same failover setup as the sibling all-spent tests above (peer
        barred at departure, active burns down), then a single tick with
        three
        variants of the active's reset -- only the active's `resets_at`
        differs across rows:

            active reset UNREPORTED  -> must BLOCK (hold: unknown != never)
            active resets in 500h    -> must SWITCH (the intended release)
            active resets in 10min   -> must BLOCK (guard still works)
        """
        cases = [
            (
                "active reset UNREPORTED (no resets_at)",
                lambda h: _usage(98.0),
                TickOutcome.BLOCKED,
                2,
            ),
            (
                "CONTROL active resets in 500h (finite, still later than peer)",
                lambda h: _usage(98.0, self._days_out(h, 500)),
                TickOutcome.SWITCHED,
                1,
            ),
            (
                "CONTROL active resets in 10min (finite, sooner than peer)",
                lambda h: _usage(98.0, self._at(h, 600)),
                TickOutcome.BLOCKED,
                2,
            ),
        ]
        for label, active_row, expected_outcome, expected_active in cases:
            h = EngineHarness(temp_home)
            h.seed(1, "a@example.com")
            h.seed(2, "b@example.com")
            h.make_live("a@example.com", 1)

            outcome = None
            for _ in range(3):  # unhealthy_ticks default is 3
                outcome = h.tick_with_usage({
                    "1": None,                              # unreadable -> failover
                    "2": _usage(4, self._at(h, 400 * 3600)),
                })
                h.clock.advance(60.0)
            assert outcome is TickOutcome.SWITCHED
            assert h.active_number() == 2

            h.clock.advance(301.0)
            out = h.tick_with_usage({
                "1": _usage(97.5, self._at(h, 400 * 3600)),  # barred peer, 400h out
                "2": active_row(h),                            # active
            })
            assert out is expected_outcome and h.active_number() == expected_active, (
                f"{label}: got {out.name}/active={h.active_number()}, want "
                f"{expected_outcome.name}/active={expected_active}"
            )

    def test_the_isfinite_guard_must_not_hold_when_a_near_peer_is_available(
        self, temp_home
    ):
        """`math.isfinite(active_recovery_ts)` reads ALL FIVE `inf` states
        as "unknown, hold" -- but two of them are
        ordinary API shapes for an active that is plainly alive and burning:
        no `resets_at` reported, or a `resets_at` already elapsed. On those
        the bar now sits on a near-spent active even when the peer is back
        within `RECOVERY_HORIZON_S` -- the same PR's own constant for "near
        enough to matter".

        Same failover setup as the sibling all-spent tests (peer barred at
        departure, active burns down), then a single tick with four
        variants -- only the ACTIVE's `resets_at` (and, for NEG, the peer's)
        differs across rows:

            POS   active reset 400h out, peer back in ~50min  -> SWITCHED
            NEG   active reset 400h out, peer only 60s sooner -> BLOCKED
            DMG-a active reset UNREPORTED, peer back in ~50min-> SWITCHED
            DMG-b active reset in the PAST, peer back in ~50min-> SWITCHED

        POS/NEG must already pass unfixed -- they pin the guard's intended
        behaviour (both controls invariant, per the review's damage table).
        DMG-a/DMG-b fail against 5c69ad2 because `isfinite` reads the
        active's `inf` as "unknown" and holds even though the peer is
        inside the horizon.
        """
        cases = [
            (
                "POS active reset 400h out, peer ~50min out",
                lambda h: _usage(98.0, self._at(h, 400 * 3600)),
                lambda h: self._at(h, 3000.0),
                TickOutcome.SWITCHED,
                1,
            ),
            (
                "NEG active reset 400h out, peer only 60s sooner",
                lambda h: _usage(98.0, self._at(h, 400 * 3600)),
                lambda h: self._at(h, 400 * 3600 - 60.0),
                TickOutcome.BLOCKED,
                2,
            ),
            (
                "DMG-a active NO resets_at, peer ~50min out",
                lambda h: _usage(98.0),
                lambda h: self._at(h, 3000.0),
                TickOutcome.SWITCHED,
                1,
            ),
            (
                "DMG-b active reset in PAST, peer ~50min out",
                lambda h: _usage(98.0, self._at(h, -3600.0)),
                lambda h: self._at(h, 3000.0),
                TickOutcome.SWITCHED,
                1,
            ),
        ]
        for label, active_row, peer_reset, expected_outcome, expected_active in cases:
            h = EngineHarness(temp_home)
            h.seed(1, "a@example.com")
            h.seed(2, "b@example.com")
            h.make_live("a@example.com", 1)

            outcome = None
            for _ in range(3):  # unhealthy_ticks default is 3
                outcome = h.tick_with_usage({
                    "1": None,                              # unreadable -> failover
                    "2": _usage(4, self._at(h, 400 * 3600)),
                })
                h.clock.advance(60.0)
            assert outcome is TickOutcome.SWITCHED
            assert h.active_number() == 2

            h.clock.advance(301.0)
            out = h.tick_with_usage({
                "1": _usage(96.0, peer_reset(h)),  # barred peer, 4 pts (below floor)
                "2": active_row(h),                # active, 2 pts
            })
            assert out is expected_outcome and h.active_number() == expected_active, (
                f"{label}: got {out.name}/active={h.active_number()}, want "
                f"{expected_outcome.name}/active={expected_active}"
            )

    def test_left_snapshot_uses_the_ranking_now_not_a_fresh_clock_read(
        self, temp_home
    ):
        """`left_snapshot` used to re-read `self.clock()` AFTER the
        ranking had already decided on a `now`, instead of reusing
        that same value. On a fake, non-advancing clock the two reads are
        identical, so no existing test could see the difference -- on a real
        wall clock any elapsed time between the two reads (however small) can
        tip a reset that was still in the future at ranking time into the
        past by the second read, turning a real `leftRecoveryAt` into `null`.

        Drives that divergence directly with a scripted clock: the value
        returned to the ranking's `now=self.clock()` sits BEFORE the active's
        binding reset; the value that a SECOND, independent `self.clock()`
        call would see (what the old code did) sits AFTER it. The recorded
        `leftRecoveryAt` must reflect the ranking-time read.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        ranking_now = 1_000_000.0
        reset_at = self._iso_at(ranking_now + 100.0)   # future at ranking_now
        stale_reread = ranking_now + 200.0             # past reset_at

        # Enough values for the OLD code's clock() call order (pre-tick
        # check, ranking now, left_snapshot re-read, freshen expiry check,
        # _perform's lastSwitchAt) with a couple of spares so neither code
        # path can exhaust the sequence.
        clock_values = iter([
            ranking_now, ranking_now, stale_reread,
            stale_reread, stale_reread, stale_reread,
        ])
        with patch.object(h.engine, "clock", side_effect=lambda: next(clock_values)):
            outcome = h.tick_with_usage({
                "1": _usage(95, reset_at),
                "2": _usage(10),
            })
        assert outcome is TickOutcome.SWITCHED
        state = h.engine._read_state()
        assert state.get("leftRecoveryAt") == ranking_now + 100.0, (
            f"leftRecoveryAt={state.get('leftRecoveryAt')!r} -- a SECOND, "
            "independent clock() read after the ranking already decided "
            "would see the reset as already past and record None; the "
            "snapshot must use the value the ranking itself decided on"
        )

    def _iso_at(self, epoch_seconds):
        from datetime import datetime, timezone

        return (
            datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def test_the_all_spent_stall_above_is_the_floor_not_the_fleet(
        self, temp_home
    ):
        """Control for the test above: strip the failover snapshot to the
        pre-upgrade shape on the IDENTICAL fleet, and it switches on the very
        next tick -- proving the stall above comes from the floor being the
        complement of all-spent, not from the fleet having nowhere to go.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        outcome = None
        for _ in range(3):
            outcome = h.tick_with_usage({
                "1": None,
                "2": _usage(4, self._at(h, 400 * 3600)),
            })
            h.clock.advance(60.0)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

        # Strip to the pre-upgrade shape: barred is named, no departure
        # evidence recorded at all -- absence releases, by design.
        h.engine._mutate_state(lambda st: st.pop("leftHeadroom", None))
        h.engine._mutate_state(lambda st: st.pop("leftRecoveryAt", None))
        assert "leftHeadroom" not in h.engine._read_state()

        h.clock.advance(301.0)
        outcome = h.tick_with_usage({
            "1": _usage(97.5, self._at(h, 300.0)),
            "2": _usage(98.0, self._days_out(h, 400)),
        })
        assert outcome is TickOutcome.SWITCHED, (
            "the identical fleet with no departure snapshot recorded "
            "switches on the very next tick -- the fleet is choosable, so "
            "the failover-shaped stall in the sibling test is the floor's "
            "own construction, not a property of the ranking"
        )

    def test_a_failover_hold_still_escapes_at_limit(self, temp_home):
        """The failover hold blocks the PROACTIVE return, not every return.

        `_no_return_account` scopes at-limit and failover out of the bar by
        design (`if trigger not in ("proactive", "consume-first"): return
        None`) — a failover-installed hold hard-blocks the proactive path via
        `_left_account_recovered`'s unconditional `return False`, but that
        predicate is never even consulted on the at-limit path. Without this
        test, a permanent-forever hold (e.g. accidentally deleting the
        trigger scope check, or a future refactor routing at-limit through
        the same predicate) would pass every other test in this class and
        strand the engine on an exhausted account with a healthy peer sitting
        right there.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        outcome = None
        for _ in range(3):  # unhealthy_ticks default is 3
            outcome = h.tick_with_usage({
                "1": None,                              # unreadable -> failover
                "2": _usage(4, self._days_out(h, 400)),
            })
            h.clock.advance(60.0)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        state = h.engine._read_state()
        assert "leftHeadroom" in state, (
            "premise: _perform writes the keys unconditionally even on failover"
        )
        assert state.get("leftHeadroom") is None
        assert state.get("leftRecoveryAt") is None

        h.clock.advance(301.0)
        outcomes = []
        for _ in range(3):
            outcomes.append(h.tick_with_usage({
                "1": _usage(96, self._days_out(h, 500)),   # frozen, 4 pts
                "2": _usage(98, self._days_out(h, 400)),   # active, burnt to 2 pts
            }))
            h.clock.advance(301.0)
        assert TickOutcome.SWITCHED not in outcomes, (
            f"{[o.name for o in outcomes]} — premise broken: the failover "
            "hold should still be blocking the proactive return here"
        )

        outcome = h.tick_with_usage({
            "1": _usage(0, self._days_out(h, 500)),     # barred, but FULL quota
            "2": _usage(100, self._days_out(h, 400)),   # active, exhausted -> at-limit
        })
        assert outcome is TickOutcome.SWITCHED, (
            "the failover hold blocked an at-limit escape onto a healthy peer "
            "— that is a permanent lockout, not the bounded hold this "
            "predicate is supposed to produce"
        )
        assert h.active_number() == 1

    def test_the_release_fires_when_the_barred_account_recovered(
        self, temp_home
    ):
        """The control: same fleet, same bar, the barred account IS better.

        Without this the assertion above would pass on a bar that never
        releases at all, which is the permanent 2-account lockout this branch
        already fixed twice. Only account 1's numbers differ — it reset to full
        quota — and that must move the engine on the recovery axis the
        emptiness retry was reaching for.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        assert h.tick_with_usage({
            "1": _usage(96, self._days_out(h, 500)),
            "2": _usage(92, self._days_out(h, 400)),
        }) is TickOutcome.SWITCHED
        h.clock.advance(3612.0)

        outcomes = []
        for _ in range(10):
            outcomes.append(h.tick_with_usage({
                "1": _usage(0, self._days_out(h, 500)),    # reset to FULL
                "2": _usage(98, self._days_out(h, 400)),
            }))
            h.clock.advance(301.0)

        assert TickOutcome.SWITCHED in outcomes, (
            f"{[o.name for o in outcomes]} — account 1 came back to full "
            "quota and is the only peer; refusing it is the permanent "
            "2-account lockout, not anti-flap"
        )

    def test_the_ratio_release_changes_where_the_engine_lands(self, temp_home):
        """`left >= active x HORIZON_HEADROOM_RATIO` — worth 50 points, unpinned.

        Measured: replacing the whole condition with `False` left the full
        suite green. It is not equivalent. `test_the_bar_never_applies_to_an_
        escape` deliberately uses 15 against 10 so the ratio CANNOT fire, and
        every other bar test releases through the emptiness path instead, so
        nothing observed the release doing its job.

        End-to-end after a 1->2 move, active 2 on 10 pts, the barred 1 on 80,
        a third peer on 30:

            release ON   -> SWITCHED to account 1 (80 pts)
            release OFF  -> SWITCHED to account 3 (30 pts)

        Asserts the DESTINATION: the tick switches either way, so an outcome
        assertion would pass with the release gone.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)

        assert h.tick_with_usage({
            "1": _usage(92, self._days_out(h, 500)),
            "2": _usage(10, self._days_out(h, 400)),
            "3": _usage(70, self._days_out(h, 300)),
        }) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        h.clock.advance(301.0)

        assert h.tick_with_usage({
            "1": _usage(20, self._days_out(h, 500)),   # barred, 80 pts
            "2": _usage(90, self._days_out(h, 400)),   # active, 10 pts
            "3": _usage(70, self._days_out(h, 300)),   # peer, 30 pts
        }) is TickOutcome.SWITCHED
        assert h.active_number() == 1, (
            f"landed on {h.active_number()} — the account we left now holds "
            "8x the active's headroom, which is a move the outbound leg would "
            "have made on its own merits, not the flip the bar refuses"
        )

    def test_the_bar_is_recomputed_on_the_phase_two_snapshot(self, temp_home):
        """Consume-first refetches, and the bar has to be re-asked.

        The two-phase commit replaces `usage`, `headroom` and `active_headroom`
        with an escalated refetch, then re-ranks — but the bar was computed
        once, before phase 1, from the STALE snapshot. `_no_return_account`'s
        ratio release consumes exactly the two values phase 2 replaces, so the
        bar is decided on data the ranking has already thrown away:

            no_return(stale: left=20, active=30) = '1'    (barred)
            no_return(fresh: left=90, active=15) = None   (released)

        Drives a real consume-first tick and swaps the snapshot underneath it:
        phase A serves the stale numbers, the phase-2 escalation (the only
        fetch that asks for every account) serves the fresh ones. On the fresh
        numbers the account we left holds 6x the active's headroom and its
        weekly window resets soonest, so it is the pick — unless the bar is
        still answering from the stale snapshot, where it lost by well under
        the ratio and stayed barred.
        """
        h = EngineHarness(temp_home, strategy="consume-first")
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)

        assert h.tick_with_usage({
            "1": _usage7(20, 20, self._days_out(h, 500)),   # active, LAST
            "2": _usage7(5, 5, self._days_out(h, 10)),      # SOONEST
            "3": _usage7(5, 5, self._days_out(h, 400)),
        }) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        h.clock.advance(301.0)

        stale = {
            "1": _usage7(80, 80, self._days_out(h, 10)),    # left; 20 pts
            "2": _usage7(70, 70, self._days_out(h, 500)),   # active; 30 pts
            "3": _usage7(60, 60, self._days_out(h, 400)),   # 40 pts
        }
        fresh = {
            "1": _usage7(10, 10, self._days_out(h, 10)),    # left; 90 pts NOW
            "2": _usage7(85, 85, self._days_out(h, 500)),   # active; 15 pts
            "3": _usage7(60, 60, self._days_out(h, 400)),   # 40 pts
        }

        def _serve(fetch=frozenset(), **kw):
            # The phase-2 escalation is the only call that asks for the whole
            # fleet; everything before it is the stale baseline.
            snap = fresh if len(fetch) >= 3 else stale
            return {n: _entry_for(v, h.clock.now) for n, v in snap.items()}

        with patch.object(
            h.switcher, "usage_entries_by_account", side_effect=_serve
        ):
            assert h.engine.tick() is TickOutcome.SWITCHED
        assert h.active_number() == 1, (
            f"landed on {h.active_number()} — on the FRESH snapshot the "
            "account we left holds 6x the active's headroom and its weekly "
            "window resets soonest, so the release fires; the bar was still "
            "answering from the stale snapshot the ranking had replaced"
        )

    def test_the_fallback_never_outranks_a_real_qualifier(self, harness):
        """It runs only when nothing else qualifies, and the key is why.

        The fallback's key is tier 0; every ordinary candidate is tier 1. So
        if a fallback entry ever reached the same list as a qualifier it would
        sort FIRST regardless of headroom. `qualifying or fallback` is the
        only thing preventing that, and nothing tested it — measured,
        replacing it with `qualifying + fallback` left the suite green while
        flipping this scenario 0/3 -> 3/3 in the fallback's favour.

        Active is spent (3 pts). One peer qualifies outright on headroom; one
        margin-failure peer resets sooner. The qualifier must win.
        """
        outcome = harness.tick_with_usage({
            "1": _usage(97, self._days_out(harness, 500)),   # active, 3 left
            "2": _usage(94, self._days_out(harness, 400)),   # 6 left: qualifies
            "3": _usage(96.4, self._days_out(harness, 100)), # 3.6 left, sooner
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2, (
            f"landed on {harness.active_number()} — the tier-0 fallback key "
            "outranked a candidate with twice the headroom"
        )

    def test_the_fallback_ranks_by_reset_not_by_headroom(self, harness):
        """`(0, recovery_ts, -h)` — the reset leads, and that is deliberate.

        Every account in the fallback is spent, and below SPENT_HEADROOM_PCT a
        headroom edge is under two poll intervals of work. The only real
        question is which account can work again first, which is the same
        judgement `_recovery_is_useful` makes one gate earlier.

        Nothing tested it: swapping to `(0, -h, recovery_ts)` left the suite
        green. Exhaustive sweep over 42336 three-account shapes, 558 change
        answer, all this shape —

            active 2.0 pts / 300h
            acct 2  2.0 pts /  10h   (spent, back soonest)
            acct 3  3.1 pts /  50h   (a point more, back 40h later)

            reset key      -> 2 first
            headroom key   -> 3 first

        Taking acct 3 buys 1.1 points, worth minutes, at the cost of 40 hours
        of waiting.
        """
        outcome = harness.tick_with_usage({
            "1": _usage(98, self._days_out(harness, 300)),    # active, 2 left
            "2": _usage(98, self._days_out(harness, 10)),     # 2 left, soonest
            "3": _usage(96.9, self._days_out(harness, 50)),   # 3.1 left, later
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2, (
            f"landed on {harness.active_number()} — took a point of spent "
            "headroom over a reset 40 hours sooner"
        )

    def test_a_materially_better_peer_still_wins(self, harness):
        """The escape must survive: 2 points left against 10 is a real move."""
        outcome = harness.tick_with_usage({
            "1": _usage(98, self._days_out(harness, 109)),   # active, 2 left
            "2": _usage(90, self._days_out(harness, 80)),    # 10 left — 5x
            "3": _usage(99, self._days_out(harness, 50)),
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2

    def test_a_minutes_away_reset_is_unaffected(self, harness):
        """Inside the horizon the recovery axis still decides, ratio or not."""
        outcome = harness.tick_with_usage({
            "1": _usage(91, self._at(harness, 7200)),
            "2": _usage(94, self._at(harness, 1800)),
            "3": _usage(98, self._at(harness, 480)),         # back in 8 min
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3


class TestTheReleasePredicateOneStateShapePerTest:
    """Every state shape `_left_account_recovered` must answer for, one test
    per shape.

    failover, peer readable and dominant AND CHANGED     -> RELEASE
    failover, peer FROZEN, active burning                -> HOLD
    failover, peer still poor                            -> HOLD
    failover, peer still unreadable                      -> HOLD
    ordinary departure, weekly-bound peer that recovered -> RELEASE
    pre-upgrade record (keys absent)                     -> RELEASE
    at-limit escape                                      -> works

    Some of these are also proven end-to-end elsewhere in this file
    (`test_a_failover_departure_does_not_disarm_the_bar` is the frozen-peer
    hold through a real tick loop,
    `test_a_switch_that_recorded_no_snapshot_still_releases` the pre-upgrade
    record, `test_a_failover_hold_still_escapes_at_limit` the at-limit
    escape); this class drives `_left_account_recovered` directly so each
    shape is checkable in isolation, against the exact state it names,
    without a multi-tick walk's other gates (cooldown, ranking, hysteresis)
    able to hide a wrong answer.
    """

    def _at(self, harness, seconds: float) -> str:
        from datetime import datetime, timezone

        return (
            datetime.fromtimestamp(harness.clock.now + seconds, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def test_failover_peer_readable_dominant_and_changed_releases(
        self, harness
    ):
        """A failover snapshot, peer now readable and clearly healthy.

        h=15: past the threshold-derived floor (`100 - 90 = 10`) but under
        20 and 50 — chosen so an absolute floor of 20 or 50 (both survived
        the mutation sweep otherwise) would give the WRONG answer (False)
        here, while the threshold-derived floor correctly releases. A peer
        at 70, past
        every plausible floor, would not tell the two apart.
        """
        state = {"lastSwitchFrom": "2", "leftHeadroom": None, "leftRecoveryAt": None}
        assert harness.engine._left_account_recovered(
            state, {"2": _usage(85)}, {"2": 15.0}, 2.0, harness.settings, harness.clock()
        ) is True, (
            "a failover departure with the peer now readable at 15 points "
            "(past the threshold-derived floor of 10, under any floor of "
            "20 or 50) must release"
        )

    def test_the_failover_floor_moves_with_the_users_threshold(self, temp_home):
        """The failover floor is `settings.threshold`-derived, not a fixed 10.

        At the default threshold (90) the floor happens to be exactly 10,
        indistinguishable from a hardcoded `h >= 10.0` (that mutation
        survived for exactly this reason). The property this branch actually
        claims is that the floor is the USER'S policy, so it
        must move when `settings.threshold` does: the SAME peer, held fixed
        at 35 points, must hold under a threshold whose floor sits above 35
        and release under one whose floor sits below it.
        """
        h_low = EngineHarness(temp_home, threshold=60.0)   # floor = 100-60 = 40
        h_low.seed(1, "a@example.com")
        h_low.seed(2, "b@example.com")
        h_low.make_live("a@example.com", 1)
        state = {"lastSwitchFrom": "2", "leftHeadroom": None, "leftRecoveryAt": None}
        assert h_low.engine._left_account_recovered(
            state, {"2": _usage(65)}, {"2": 35.0}, 2.0, h_low.settings, h_low.clock()
        ) is False, (
            "threshold=60 -> floor=40; a peer at 35 points is BELOW that "
            "floor and must hold"
        )

        h_high = EngineHarness(temp_home, threshold=71.0)  # floor = 100-71 = 29
        h_high.seed(1, "a@example.com")
        h_high.seed(2, "b@example.com")
        h_high.make_live("a@example.com", 1)
        assert h_high.engine._left_account_recovered(
            state, {"2": _usage(65)}, {"2": 35.0}, 2.0, h_high.settings, h_high.clock()
        ) is True, (
            "the SAME peer at 35 points, only `settings.threshold` changed "
            "(71 -> floor 29) — a hardcoded absolute floor would answer the "
            "same both times; this must flip, proving the floor is the "
            "user's own policy, not a fixed constant"
        )

    def test_failover_peer_frozen_active_burning_holds(self, temp_home):
        """A failover snapshot, peer frozen, only the active moves.

        End-to-end walk, not a direct predicate call: this is the exact
        shape an earlier cut shipped broken, so it is worth proving through
        the real tick loop rather than only the unit. See
        `test_a_failover_departure_does_not_disarm_the_bar` for the longer
        walk this is a focused version of.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)

        outcome = None
        for _ in range(3):
            outcome = h.tick_with_usage({
                "1": None,
                "2": _usage(4, self._at(h, 400 * 3600)),
            })
            h.clock.advance(60.0)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

        h.clock.advance(301.0)
        outcome = h.tick_with_usage({
            "1": _usage(96, self._at(h, 500 * 3600)),   # frozen, 4 pts, below floor
            # active burnt past the bare-ratio boundary
            "2": _usage(98.2, self._at(h, 400 * 3600)),
        })
        assert outcome is not TickOutcome.SWITCHED, (
            "the peer never changed and is well under the threshold-derived "
            "floor; burning the active alone must not release a failover hold"
        )

    def test_failover_peer_still_poor_holds(self, harness):
        """A failover snapshot, peer readable but genuinely poor."""
        state = {"lastSwitchFrom": "2", "leftHeadroom": None, "leftRecoveryAt": None}
        # 5.0 < 100 - 90 = 10, the threshold-derived floor: readable, but poor.
        assert harness.engine._left_account_recovered(
            state, {"2": _usage(95)}, {"2": 5.0}, 2.0, harness.settings, harness.clock()
        ) is False, (
            "a failover departure with the peer readable but under the "
            "floor must hold — readable is not the same as recovered"
        )

    def test_failover_peer_still_unreadable_holds(self, harness):
        """A failover snapshot, peer still unreadable."""
        state = {"lastSwitchFrom": "2", "leftHeadroom": None, "leftRecoveryAt": None}
        assert harness.engine._left_account_recovered(
            state, {"2": None}, {"2": None}, 2.0, harness.settings, harness.clock()
        ) is False, (
            "a failover departure with the peer still unreadable must hold; "
            "unknown is not evidence of recovery"
        )

    def test_ordinary_departure_weekly_bound_peer_that_recovered_releases(
        self, harness
    ):
        """A real (non-failover) baseline, and the peer's WEEKLY window
        actually rolled over — genuine recovery on the recovery axis, which
        the headroom axis (pinned by 7-day utilization) cannot see at all.
        """
        state = {
            "lastSwitchFrom": "2",
            "leftHeadroom": 4.0,
            "leftRecoveryAt": harness.clock.now + 3600.0,  # was back in 1h
        }
        # Same 4.0 pts as departure (headroom leg cannot fire), but the
        # binding reset is now well past the old one plus the hysteresis
        # margin — a real recovery event, not drift.
        assert harness.engine._left_account_recovered(
            state,
            {"2": _usage(96, self._at(harness, 60.0))},  # now back in 1 MINUTE
            {"2": 4.0},
            8.0,
            harness.settings,
            harness.clock(),
        ) is True, (
            "the peer's weekly-bound reset moved meaningfully nearer, which "
            "is a real recovery event the headroom leg cannot see"
        )

    def test_pre_upgrade_record_keys_absent_releases(self, harness):
        """State written before the snapshot fields existed."""
        state = {"lastSwitchFrom": "2"}  # no leftHeadroom / leftRecoveryAt key
        assert harness.engine._left_account_recovered(
            state, {"2": _usage(96)}, {"2": 4.0}, 2.0, harness.settings, harness.clock()
        ) is True, (
            "absence of the snapshot fields (a pre-upgrade record) carries "
            "no evidence either way and must release, not hold forever"
        )

    def test_at_limit_escape_still_works(self, harness):
        """At-limit skips this predicate's bar entirely by trigger scope.

        `_no_return_account` (not `_left_account_recovered`) is what gates
        at-limit/failover out; this confirms the fix did not touch that
        scoping. See `test_a_failover_hold_still_escapes_at_limit` for the
        longer end-to-end version through a real failover-then-at-limit walk.
        """
        state = {"lastSwitchFrom": "2"}
        headroom = {"1": 0.0, "2": 100.0}
        for active in (0.0, None):
            for recovered in (True, False):
                assert harness.engine._no_return_account(
                    "at-limit", state, headroom, active, recovered, harness.settings
                ) is None, (
                    "at-limit must escape the bar regardless of "
                    "active_headroom or the recovered predicate's answer"
                )


class TestAllSpentGoesToTheSoonestReset:
    """When every account is spent, sit where the quota comes back first.

    Headroom decides while there is headroom worth comparing. Once everyone is
    down to a point or two, headroom says nothing — a one-point edge is under
    ten minutes of work at the burn rates measured on 2026-07-30 — and the only
    thing that still matters is who returns first, so the reset finds us
    already on it.

    The horizon rule alone got this wrong: past four hours it always ranked by
    headroom, so three accounts at 99% had no qualifying candidate and the
    engine parked on whichever one it happened to be on — including the one
    resetting LAST, 109h out against a peer 50h out.
    """

    def _at(self, harness, seconds: float) -> str:
        from datetime import datetime, timezone

        return (
            datetime.fromtimestamp(harness.clock.now + seconds, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def test_all_spent_moves_to_the_soonest_reset(self, harness):
        """The reported shape: 99/99/99, days out, active resets last."""
        outcome = harness.tick_with_usage({
            "1": _usage(99, self._at(harness, 109 * 3600)),  # active, LAST
            "2": _usage(99, self._at(harness, 80 * 3600)),
            "3": _usage(99, self._at(harness, 50 * 3600)),   # SOONEST
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3, (
            "parked on the account that returns last while a peer comes back "
            "59h sooner"
        )

    def test_already_on_the_soonest_stays_put(self, harness):
        """No move when we are already where the quota returns first."""
        outcome = harness.tick_with_usage({
            "1": _usage(99, self._at(harness, 50 * 3600)),   # active, SOONEST
            "2": _usage(99, self._at(harness, 80 * 3600)),
            "3": _usage(99, self._at(harness, 109 * 3600)),
        })
        assert outcome is not TickOutcome.SWITCHED
        assert harness.active_number() == 1

    def test_real_headroom_still_beats_a_sooner_reset(self, harness):
        """Above the spent band the headroom axis still rules: a peer holding
        ten points wins even though a spent one resets sooner."""
        outcome = harness.tick_with_usage({
            "1": _usage(98, self._at(harness, 109 * 3600)),  # active, 2 left
            "2": _usage(90, self._at(harness, 80 * 3600)),   # 10 left
            "3": _usage(99, self._at(harness, 50 * 3600)),   # 1 left, soonest
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2

    def test_a_spent_fleet_takes_the_soonest_reset_over_the_most_headroom(
        self, harness
    ):
        """`_recovery_is_useful`'s spent clause, as a whole, was unpinned.

        Its two legs are individually killed
        (`test_a_peer_with_real_headroom_still_wins_past_the_horizon`,
        `test_an_unknown_active_reset_keeps_the_headroom`), but removing the
        entire `if` — the clause's whole stated purpose — left the full suite
        green. Reachable through `tick()`:

            active 1: 0.5 pts, 300h out
            peer   2: 0.5 pts, back in 10h
            peer   3: 1.0 pt,  500h out

            ORIGINAL -> SWITCHED to 2 (the 10h account)
            MUTANT   -> SWITCHED to 3 (the 500h account)

        Every account is under SPENT_HEADROOM_PCT, which is exactly the regime
        the clause exists for: at half a point a headroom edge is minutes of
        work, so the only real question is who returns first. Without the
        clause the axis falls back to headroom, and one extra point buys a
        490-hour wait.

        Asserts the DESTINATION — both answers are a switch.
        """
        outcome = harness.tick_with_usage({
            "1": _usage(99.5, self._at(harness, 300 * 3600)),  # active, 0.5 pt
            "2": _usage(99.5, self._at(harness, 10 * 3600)),   # 0.5 pt, SOON
            "3": _usage(99.0, self._at(harness, 500 * 3600)),  # 1.0 pt, LAST
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2, (
            f"landed on {harness.active_number()} — every account is spent, "
            "so half a point of extra headroom bought a 490-hour wait over an "
            "account back in ten"
        )

    def test_the_flap_guard_survives_in_the_spent_band(self, harness):
        """Ranking by reset must not reintroduce ping-pong: an account whose
        reset is barely sooner does not qualify."""
        outcome = harness.tick_with_usage({
            "1": _usage(99, self._at(harness, 50 * 3600)),        # active
            "2": _usage(99, self._at(harness, 50 * 3600 - 60)),   # 60s sooner
            "3": _usage(99, self._at(harness, 80 * 3600)),
        })
        assert outcome is not TickOutcome.SWITCHED


class TestEscapeBeforeTheLimitLands:
    """At the brink the ordinary proactive path already leaves — verified.

    I assumed ``at-limit`` firing only at exactly 0% meant an account rode to
    100% before escaping, and set out to move the trigger a point earlier.
    Measuring it refuted that: at 99% with a peer that has real headroom, the
    engine switches on the ORDINARY proactive path, because 99% is above the
    threshold and the peer clears the hysteresis margin easily.

    What actually happened in the 18:50 observation that prompted this: the
    only peers were 99% (one point) and 100% (never a target), so there was
    nowhere better and holding was correct. The spent check already covers that
    case by ranking on the soonest reset.

    Kept as a regression pin: moving the at-limit trigger earlier looks
    appealing and is wrong — it hijacks the recovery ranking (#202) and the
    spent-band ranking, both of which belong to `proactive`.
    """

    def _at(self, harness, seconds: float) -> str:
        from datetime import datetime, timezone

        return (
            datetime.fromtimestamp(harness.clock.now + seconds, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def test_at_99_the_proactive_path_already_escapes(self, harness):
        """No special trigger needed: 99% is over the threshold and a healthy
        peer clears the margin."""
        outcome = harness.tick_with_usage({
            "1": _usage(99, self._at(harness, 109 * 3600)),  # active, 1 left
            "2": _usage(70, self._at(harness, 80 * 3600)),   # 30 left
            "3": _usage(100, self._at(harness, 50 * 3600)),
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2
        sw = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "proactive", (
            "at-limit must stay bound to headroom <= 0: it skips the recovery "
            "and spent-band rankings that proactive owns"
        )

    def test_at_99_with_only_spent_peers_it_holds(self, harness):
        """The 18:50 shape: nowhere better, so staying is right. The spent-band
        rule decides where to sit, not an early escape."""
        outcome = harness.tick_with_usage({
            "1": _usage(99, self._at(harness, 109 * 3600)),
            "2": _usage(100, self._at(harness, 80 * 3600)),
            "3": _usage(100, self._at(harness, 50 * 3600)),
        })
        assert outcome is not TickOutcome.SWITCHED

    def test_below_the_brink_the_ordinary_rules_still_decide(self, harness):
        """A comfortable account is untouched: the hysteresis margin applies."""
        outcome = harness.tick_with_usage({
            "1": _usage(50, self._at(harness, 109 * 3600)),
            "2": _usage(45, self._at(harness, 80 * 3600)),
            "3": _usage(40, self._at(harness, 50 * 3600)),
        })
        assert outcome is not TickOutcome.SWITCHED


class TestReviewFindings202:
    """Three defects found reviewing #202, each reproduced before fixing.

    All three shared a cause worth naming: the code was written against the
    interval I happened to run (360s) and the account shapes I happened to
    test, not against the configurable range or the trigger matrix.
    """

    def _at(self, harness, seconds: float) -> str:
        from datetime import datetime, timezone

        return (
            datetime.fromtimestamp(harness.clock.now + seconds, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def test_a_short_interval_is_never_lengthened(self, harness):
        """The default is 60s and the floor is 15s, not the 360s I developed
        against. max(min(delay, due_in), URGENT) RAISES a delay already below
        URGENT, so a 15s interval slept 60s — the exact opposite of the
        'only ever shortens' invariant this function claims."""
        num = harness.engine.switcher.current_account_number()
        real = harness.engine.switcher.usage_entries_by_account

        def patched(fetch=frozenset(), **kw):
            entries = dict(real(fetch=fetch, **kw))
            entries[num] = replace(entries[num], next_poll_at=harness.clock() + 5.0)
            return entries

        harness.engine.switcher.usage_entries_by_account = patched
        harness.engine.settings = replace(
            harness.engine.settings, interval_seconds=15.0
        )
        delay = harness.engine._next_delay(TickOutcome.NO_ACTION)
        assert delay <= 15.0 * 1.1, f"a 15s interval slept {delay:.1f}s"

    def test_recovery_reads_the_binding_windows_reset(self, harness):
        """Filtering unusable resets BEFORE taking the max let a lower window
        answer for the account: 7d at 95% with no reset and 5h at 40% resetting
        in an hour reported 'back in an hour', which is not what binds."""
        from claude_swap.autoswitch import _binding_recovery_ts

        now = harness.clock()
        usage = {
            "five_hour": {"pct": 40.0, "resets_at": self._at(harness, 3600)},
            "seven_day": {"pct": 95.0},  # binding, and no reset we can use
        }
        assert _binding_recovery_ts(usage, (), now) == float("inf")

    def test_at_limit_still_ranks_by_headroom_when_all_are_above(self, harness):
        """The gate was scoped to proactive/consume-first; the KEY was not, so
        at-limit silently re-ranked by soonest-recovery. My earlier at-limit
        test missed it because its healthy candidate made all_above False —
        this one keeps every account above the line, which is the combination
        that reaches the key."""
        outcome = harness.tick_with_usage({
            "1": _usage(100, self._at(harness, 60)),    # active, at its limit
            "2": _usage(91, self._at(harness, 86400)),  # most headroom, far reset
            "3": _usage(97, self._at(harness, 120)),    # soonest back, less room
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2, (
            "at-limit must take the most headroom, not the soonest recovery"
        )
        sw = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "at-limit"

class TestFreshenRoutesThroughGate:
    """M2: autoswitch's freshen no longer POSTs a raw snapshot — it routes
    through the switcher's consume gate (locked re-read + CAS persist)."""

    def test_lock_contention_is_not_reported_as_network_trouble(
        self, temp_home
    ):
        """Waiting on another gate is local, not a connection problem.

        The consume lock serializes gates per slot; a loser defers. That is the
        design working, and on a machine where the collector and a manual
        `cswap switch` overlap it happens routinely. Reporting it as "could not
        freshen any candidate (network?)" sends the user to check a connection
        that is fine, for a condition no network change can affect.
        """
        from claude_swap import oauth as oauth_mod

        harness = EngineHarness(temp_home)
        harness.seed(2, "b@example.com", expires_at=1)
        with patch.object(
            harness.switcher, "consume_backup_grant",
            return_value=oauth_mod.RefreshOutcome(None, "consume-busy"),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "consume-busy", (
            f"got {status!r}: lock contention falls into the transient bucket "
            "and reads as (network?)"
        )

    def test_invalid_client_is_not_reported_as_network_trouble(
        self, temp_home
    ):
        """A rejected OAuth client must keep its own kind.

        oauth.py splits ``invalid_client`` out from ``invalid_grant`` precisely
        because it says nothing about any slot's refresh token — OUR client
        credential was rejected, which is systemic and deterministic. But
        _freshen_target maps every unrecognised kind to "transient", and a
        transient freshen failure surfaces as "could not freshen any candidate
        (network?)". A client_id rotation or block would then present as
        intermittent network trouble on every machine at once, with nothing
        naming the real cause — the same trap ``store-unmirrored`` was given
        its own kind to escape.
        """
        from claude_swap import oauth as oauth_mod

        harness = EngineHarness(temp_home)
        harness.seed(2, "b@example.com", expires_at=1)
        with patch.object(
            harness.switcher, "consume_backup_grant",
            return_value=oauth_mod.RefreshOutcome(None, "invalid_client"),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "invalid_client", (
            f"got {status!r}: a systemic client rejection falls into the "
            "transient bucket and reads as (network?)"
        )

    def test_unreadable_stash_is_not_reported_as_network_trouble(
        self, temp_home
    ):
        """A permanently unreadable stash row is local and needs a human.

        The row is the sole copy of a generation the slot already consumed, so
        the gate defers on every pass — correctly, since nothing on disk tells
        a keychain locked for a minute from one locked forever. But an
        unrecognised kind maps to "transient", and the tick then renders
        "could not freshen any candidate (network?)" forever, on a condition
        no network change can affect and only the operator can clear.
        """
        from claude_swap import oauth as oauth_mod

        harness = EngineHarness(temp_home)
        harness.seed(2, "b@example.com", expires_at=1)
        with patch.object(
            harness.switcher, "consume_backup_grant",
            return_value=oauth_mod.RefreshOutcome(None, "stash-unreadable"),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "stash-unreadable", (
            f"got {status!r}: an unreadable stash row falls into the "
            "transient bucket and reads as (network?)"
        )

    def test_store_unmirrored_keeps_its_own_kind(self, temp_home):
        """The precedent this mirrors, pinned so the two stay symmetric."""
        from claude_swap import oauth as oauth_mod

        harness = EngineHarness(temp_home)
        harness.seed(2, "b@example.com", expires_at=1)
        with patch.object(
            harness.switcher, "consume_backup_grant",
            return_value=oauth_mod.RefreshOutcome(None, "store-unmirrored"),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "store-unmirrored"

    def test_an_actionable_cause_is_not_hidden_by_a_self_clearing_one(
        self, temp_home
    ):
        """The tick reports ONE systemic cause, and it must be the actionable one.

        ``systemic`` was assigned unconditionally per candidate, so the LAST
        one won. ``consume-busy`` clears itself on the next pass; the other two
        need a human (unset an env var, chase a rejected client_id). Whenever a
        busy slot sorted after an unmirrored one, the message named the harmless
        cause and the real one was invisible — the same "reads as intermittent,
        nothing names the cause" trap these kinds were split out to escape.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=1)
        h.seed(3, "c@example.com", expires_at=1)
        h.make_live("a@example.com", 1)

        # Slot 2 needs a human (an env var is set); slot 3 clears itself.
        def by_slot(num, email, *a, **kw):
            return "store-unmirrored" if num == "2" else "consume-busy"

        with patch.object(
            h.engine, "_freshen_target", side_effect=by_slot
        ):
            h.tick_with_usage({
                "1": _usage7(95, 95, _R_LATER),   # active, over threshold
                "2": _usage7(10, 10, _R_SOON),
                "3": _usage7(10, 10, _R_LATEST),
            })

        errors = [e for e in h.events if getattr(e, "message", None)]
        assert errors, f"no error event; got kinds {h.kinds()}"
        msg = errors[-1].message
        assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" in msg, (
            f"got {msg!r}: the self-clearing cause hid the one needing a human"
        )

    def test_a_real_transient_still_reads_transient(self, temp_home):
        from claude_swap import oauth as oauth_mod

        harness = EngineHarness(temp_home)
        harness.seed(2, "b@example.com", expires_at=1)
        with patch.object(
            harness.switcher, "consume_backup_grant",
            return_value=oauth_mod.RefreshOutcome(None, "transient"),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "transient"

    def test_freshen_calls_consume_gate(self, temp_home, monkeypatch):
        from claude_swap import oauth as oauth_mod
        harness = EngineHarness(temp_home)
        harness.seed(2, "b@example.com", expires_at=1)  # near-expiry
        eng = harness.engine
        gate_calls = {}
        fresh = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-y", "refreshToken": "rt-y",
                "expiresAt": 9999999999000,
            }
        })

        def gate(num, email, snapshot):
            gate_calls["args"] = (num, email, snapshot)
            return oauth_mod.RefreshOutcome(fresh, None)

        harness.switcher.consume_backup_grant = gate
        direct = {}
        def direct_post(*a, **k):
            direct["called"] = True
            return oauth_mod.RefreshOutcome(None, "transient")

        monkeypatch.setattr(
            oauth_mod, "try_refresh_oauth_credentials", direct_post
        )
        verdict = eng._freshen_target("2", "b@example.com")
        assert verdict == "ok"
        assert gate_calls["args"][0] == "2"
        assert "called" not in direct, "freshen must not POST outside the gate"

