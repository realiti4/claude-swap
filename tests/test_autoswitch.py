"""Tests for the auto-switch engine (autoswitch.py)."""

from __future__ import annotations

import functools
import hashlib
import io
import json
import logging
import os
import random
import subprocess
import sys
import tarfile
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
    _seven_day_reset_ts,
    classify_candidate_block,
    pct_label,
)
from claude_swap.exceptions import ClaudeSwitchError
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


def _seed_org_twin(h, num, email, org):
    """Seed a slot, then give its roster row an organizationUuid."""
    h.seed(num, email)
    d = h.switcher._get_sequence_data()
    d["accounts"][str(num)]["organizationUuid"] = org
    h.switcher._write_json(h.switcher.sequence_file, d)


def _blind_quarantine(h, num, email, reason="identity-conflict"):
    store = h.switcher._store
    real = store._read_account_credentials

    def unreadable(account_num, e, failed=None):
        if account_num == num:
            if failed is not None:
                failed.append(True)
            return ""
        return real(account_num, e, failed)

    with patch.object(store, "_read_account_credentials", side_effect=unreadable):
        h.engine._quarantine(num, email, reason)


class EngineHarness:
    """Seeded switcher + engine + captured events, on the Linux file backend."""

    def __init__(self, temp_home: Path, *, engine_cls=None, **settings_kwargs):
        self.temp_home = temp_home
        # Base-vs-head comparison (TestOutcomeDigestAgainstBase) drives the
        # SAME switcher/settings/clock through a different `AutoSwitchEngine`
        # class (a pre-fix source loaded separately) — this is the one hook
        # that needs, so the harness itself stays a single implementation.
        self._engine_cls = engine_cls or AutoSwitchEngine
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
        harness.engine.settings = replace(harness.engine.settings, strategy="best")
        outcome = harness.tick_with_usage({
            "1": _usage(50), "2": _usage(10), "3": _usage(10),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert harness.active_number() == 1
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_over_threshold_switches_to_max_headroom(self, harness):
        harness.engine.settings = replace(harness.engine.settings, strategy="best")
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
        harness.engine.settings = replace(harness.engine.settings, strategy="best")
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

    def test_the_active_does_not_take_the_wall_while_a_peer_can_serve(
        self, temp_home
    ):
        """Reaching the limit PINS every session on that account.

        A session that hits a session limit keeps retrying the account it was
        on: Claude Code rebuilds its client on 401/403 and socket errors and
        never on 429, so a switch afterwards reaches new requests only.
        Measured on a live fleet — a session bound to a slot whose 5-hour
        window returned two hours later sat idle while the active account
        carried 76% headroom.

        So "the active comes back soonest" must not buy riding it to 100% when
        a peer can still take work. The peer here returns LATER on purpose:
        under the recovery rule alone the engine would stay.
        """
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        active = _usage7(99.0, 40.0)
        active["five_hour"]["resets_at"] = _iso_at(now + 5 * 60)
        peer = _usage7(30.0, 95.0)
        peer["five_hour"]["resets_at"] = _iso_at(now + 3 * 3600)
        outcome = h.tick_with_usage({"1": active, "2": peer})
        assert outcome is TickOutcome.SWITCHED, (
            "the active is one point from the wall and the peer can still "
            "serve; staying pins every in-flight session on the account that "
            "is about to stop answering"
        )
        assert h.active_number() == 2

    def test_the_wall_is_taken_on_the_account_that_lifts_first(self, temp_home):
        """When nothing can serve, choose WHERE to be stuck.

        A session that reaches a session limit is pinned to the account it was
        on — Claude Code rebuilds its client on 401/403 and socket errors and
        never on 429 — so the banner clears when THAT account's window returns,
        whatever the fleet does afterwards. With every account spent the wall
        is unavoidable; being behind the one that lifts first is the whole
        difference between minutes and hours.

        The peer here is itself at its limit, which the ordinary rule excludes
        as a target outright. It is still the right place to be: it returns in
        ten minutes and the active does not return for three hours.
        """
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        active = _usage7(100.0, 60.0)
        active["five_hour"]["resets_at"] = _iso_at(now + 3 * 3600)
        soonest = _usage7(100.0, 60.0)
        soonest["five_hour"]["resets_at"] = _iso_at(now + 10 * 60)
        outcome = h.tick_with_usage({"1": active, "2": soonest})
        assert outcome is TickOutcome.SWITCHED, (
            "both accounts are spent, so the wall is coming either way; the "
            "peer lifts in ten minutes and the active in three hours, and the "
            "session is pinned to whichever it is on when the limit lands"
        )
        assert h.active_number() == 2

    def test_a_spent_peer_that_lifts_later_is_still_refused(self, temp_home):
        """Landing on a spent account is only ever justified by a SOONER
        return; without that it is a strictly worse place to be stuck.

        NOT the control for the spent guard, though it was labelled one: on
        `at-limit` the ranking loop applies the same margin a few lines below,
        so this case passes with the guard deleted outright, and passes on the
        commit before the guard existed. What actually pins the guard is
        `test_a_disabled_active_keeps_the_recovery_margin`, on the one trigger
        that skips that second check.
        """
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        active = _usage7(100.0, 60.0)
        active["five_hour"]["resets_at"] = _iso_at(now + 10 * 60)
        later = _usage7(100.0, 60.0)
        later["five_hour"]["resets_at"] = _iso_at(now + 3 * 3600)
        outcome = h.tick_with_usage({"1": active, "2": later})
        assert outcome is not TickOutcome.SWITCHED
        assert h.active_number() == 1

    def test_a_spent_peer_never_beats_a_peer_that_can_still_serve(
        self, temp_home
    ):
        """THE OTHER CONTROL, and the one the relaxation's own gate misses.

        Landing on a spent account is justified only when NOTHING can serve --
        then the wall is coming either way and the choice is where to be stuck.
        `all_above` does not say that: every account being at/over the
        THRESHOLD leaves room for a peer holding real quota, and one that can
        still take work is strictly better than one that answers nothing for
        the next ten minutes.

        The two are separated on the escape ranking, where the spent peer wins
        on a window that is not the one blocking it: `headroom_on_window` ranks
        order only, and says so -- one clear window is not a usability test,
        so the caller has to decide that separately. Admission is where this
        case decides it.
        """
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        # Blocked on the WEEKLY window, days out: the escape ranks on "7d".
        active = _usage7(50.0, 100.0, _iso_at(now + 3 * 86400))
        # Spent on its 5-hour window and back in ten minutes -- sooner than the
        # active, so the spent relaxation admits it -- but holding half a week
        # of quota, so the "7d" escape key scores it far above the peer.
        spent = _usage7(100.0, 50.0)
        spent["five_hour"]["resets_at"] = _iso_at(now + 10 * 60)
        # Five points on both windows: over the threshold, so `all_above`
        # still holds, and able to answer a request right now.
        servable = _usage7(95.0, 95.0)
        outcome = h.tick_with_usage(
            {"1": active, "2": spent, "3": servable}
        )
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3, (
            "the engine took an account whose five-hour window is at 100% "
            "over one with five points of real headroom -- the session is "
            "pinned to whatever it lands on, so this buys ten minutes of "
            "answering nothing in exchange for a peer that could serve now"
        )

    def test_two_spent_accounts_do_not_alternate_across_ticks(self, temp_home):
        """The wall move is one-way, and it is the only rule here that ticks.

        `at-limit` skips the no-return bar by design, so nothing but the
        recovery hysteresis stands between "move to whoever lifts first" and a
        pair trading places every poll. Ticking is the whole point: every other
        case in this group observes the outbound leg only, which is exactly how
        an oscillation gets certified as a move.

        The guard is doubled, which this test is what showed: removing the
        spent guard's own margin alone leaves the ranking loop's margin, and
        the pair still holds. Removing BOTH makes this flap back on tick 2 --
        that is the control that gives the seven quiet ticks below any meaning.
        """
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        one = _usage7(100.0, 60.0)
        one["five_hour"]["resets_at"] = _iso_at(now + 3 * 3600)
        two = _usage7(100.0, 60.0)
        two["five_hour"]["resets_at"] = _iso_at(now + 10 * 60)
        assert h.tick_with_usage({"1": one, "2": two}) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        for i in range(6):
            h.clock.advance(60)
            out = h.tick_with_usage({"1": one, "2": two})
            assert h.active_number() == 2, f"flapped back on tick {i + 2}: {out}"

    def test_the_wall_that_is_coming_is_not_only_the_five_hour_one(
        self, temp_home
    ):
        """"About to stop answering" is the BINDING window, not the 5-hour one.

        The active here is two points from its WEEKLY limit and wide open on
        its five-hour one, so a five-hour-only read calls it healthy and the
        engine sits until the wall lands -- pinning every session on it for the
        rest of that window. The peer holds eight points and its own weekly
        reset is ten days out, so there is somewhere to go.

        The same blind spot under `--models`: a pinned model's scoped weekly
        window reads 0.0 headroom on `account_headroom` while the five-hour
        figure still reports 100.0. `h` sees both; a five-hour read sees
        neither.
        """
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        active = _usage7(0.0, 98.0, _iso_at(now + 3 * 3600))
        peer = _usage7(5.0, 92.0, _iso_at(now + 10 * 86400))
        outcome = h.tick_with_usage({"1": active, "2": peer})
        assert outcome is TickOutcome.SWITCHED, outcome
        assert h.active_number() == 2

    def test_a_pinned_models_own_wall_is_a_wall(self, temp_home):
        """The `--models` half of the same blind spot, through `tick()`.

        With a model pinned, that model's weekly window gates the work as hard
        as the five-hour one. `account_headroom` folds it in; a five-hour-only
        read of the same row reports 100.0 points free.

        The scoped window sits at 98, NOT at 100, on purpose. At 100 the active
        is at-limit and the ordinary escape moves the engine whatever this rule
        says -- a version of this case written that way passed with the
        pre-fix axis restored, proving nothing. Two points short, the trigger
        is `proactive`, the peer's own reset is ten days out, and the recovery
        ordering says stay: this rule is the only thing that can move it.
        """
        h = EngineHarness(
            temp_home, threshold=90.0, hysteresis_pct=5.0, model="Fable"
        )
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        active = {
            "five_hour": {"pct": 0.0},
            "seven_day": {"pct": 10.0},
            "scoped": [{"name": "Fable", "pct": 98.0,
                        "resets_at": _iso_at(now + 3 * 3600)}],
        }
        peer = {
            "five_hour": {"pct": 5.0},
            "seven_day": {"pct": 10.0},
            "scoped": [{"name": "Fable", "pct": 92.0,
                        "resets_at": _iso_at(now + 9 * 86400)}],
        }
        outcome = h.tick_with_usage({"1": active, "2": peer})
        assert outcome is TickOutcome.SWITCHED, (
            "the active is two points from its pinned model's weekly wall "
            "and the peer holds eight -- a five-hour-only read calls the "
            f"active healthy and rides it into the wall: {outcome}"
        )
        assert h.active_number() == 2

    def _spent_fleet(self, temp_home, *, lifts_in):
        """Four slots, every one at its 5-hour limit, each lifting when told."""
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0)
        for num, email in enumerate(("a", "b", "c", "d")[: len(lifts_in)], 1):
            h.seed(num, f"{email}@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        usage = {}
        for num, mins in enumerate(lifts_in, 1):
            row = _usage7(100.0, 60.0)
            row["five_hour"]["resets_at"] = _iso_at(now + mins * 60)
            usage[str(num)] = row
        return h, usage

    def test_a_disabled_active_lands_on_the_peer_that_lifts_first(
        self, temp_home
    ):
        """`disabled-active` admits a spent peer on a RECOVERY argument, so it
        has to rank on one too.

        That trigger is not in `by_recovery_axis`, so the tiered recovery key
        is not used and the escape key is reached with no `escape_label` --
        `-h`, which is 0 for every spent account, leaving sequence order to
        pick. The engine then takes the lowest slot number, which is the exact
        inversion of the rule that admitted it.
        """
        h, usage = self._spent_fleet(temp_home, lifts_in=(60, 50, 10))
        h.switcher.set_account_disabled("1", True)
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3, (
            "slot 3 lifts in ten minutes and slot 2 in fifty; landing on 2 "
            "means the tie fell to slot order on the one trigger whose whole "
            "argument for moving was the return time"
        )

    def test_a_disabled_active_keeps_the_recovery_margin(self, temp_home):
        """THE MARGIN'S ONLY LOAD-BEARING PATH.

        On `at-limit` the ranking loop re-applies the same
        RECOVERY_HYSTERESIS_S test a few lines below, so deleting it from the
        spent guard changes nothing there. `disabled-active` skips that whole
        block, so here the guard's own margin is the only thing standing
        between "lifts first" and a peer four minutes sooner.
        """
        h, usage = self._spent_fleet(temp_home, lifts_in=(60, 56))
        h.switcher.set_account_disabled("1", True)
        assert h.tick_with_usage(usage) is not TickOutcome.SWITCHED, (
            "four minutes is inside RECOVERY_HYSTERESIS_S; taking it trades a "
            "credential rewrite for nothing and re-opens the flap the margin "
            "exists to bound"
        )
        assert h.active_number() == 1

    def test_an_active_that_is_not_about_to_wall_keeps_the_work(
        self, temp_home
    ):
        """THE SCOPING OF THE WALL RULE, which nothing else pins.

        Rule 1 bypasses the recovery hysteresis, so without `about_to_wall`
        it would fire whenever ANY peer can serve -- abandoning an active that
        is back in ten minutes for one that is back in four hours. Deleting
        `about_to_wall` alone passes the rest of this file.
        """
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        active = _usage7(94.0, 60.0)
        active["five_hour"]["resets_at"] = _iso_at(now + 10 * 60)
        peer = _usage7(92.0, 60.0)
        peer["five_hour"]["resets_at"] = _iso_at(now + 4 * 3600)
        outcome = h.tick_with_usage({"1": active, "2": peer})
        assert outcome is not TickOutcome.SWITCHED, (
            "the active holds six points and is back in ten minutes; the peer "
            f"holds eight and is back in four hours: {outcome}"
        )
        assert h.active_number() == 1

    def test_being_further_over_a_limit_does_not_outrank_lifting_first(
        self, temp_home
    ):
        """A tie the API is not obliged to give us.

        `utilization` is copied through unclamped, so a spent account can read
        100.5 and score BELOW one at exactly 100.0 on the escape key -- half a
        point, which this module calls noise, deciding against the return time
        it calls the only real question. Ranking spent candidates by reset
        only works if they actually tie, so the score is clamped.
        """
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0)
        for num, email in enumerate(("a", "b", "c"), 1):
            h.seed(num, f"{email}@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now

        def row(pct5, mins):
            r = _usage7(pct5, 60.0)
            r["five_hour"]["resets_at"] = _iso_at(now + mins * 60)
            return r

        h.switcher.set_account_disabled("1", True)
        outcome = h.tick_with_usage(
            {"1": row(100.0, 60), "2": row(100.0, 50), "3": row(100.5, 10)}
        )
        assert h.active_number() == 3, (
            f"landed on {h.active_number()} ({outcome}): slot 3 is half a "
            "point further over its limit and lifts in ten minutes, slot 2 in "
            "fifty -- the half point is not a reason to wait forty more"
        )

    def test_an_unreadable_active_never_admits_a_spent_peer(self, temp_home):
        """`active_headroom is None` is not `active_headroom == 0`.

        On failover there is no measured active to rank a return against, so a
        spent peer must stay refused. This case pins the guard as a WHOLE --
        delete it and this fails -- not any one conjunct: `all_above`
        short-circuits first, but with it neutered the margin still refuses,
        because both recovery values are the 0.0 sentinel outside it. No test
        here can separate the three.
        """
        h, usage = self._spent_fleet(temp_home, lifts_in=(60, 10))
        usage["1"] = None                      # unreadable, not spent
        for _ in range(2):
            assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        outcome = h.tick_with_usage(usage)
        assert h.active_number() == 1, (
            "failover took a peer that is itself at its limit: it can serve "
            f"nothing, and nothing measured the active it beat: {outcome}"
        )

    def test_consume_first_does_not_rank_a_disabled_escape_on_the_weekly(
        self, temp_home
    ):
        """The same inversion, one ranking arm over.

        `consume-first` is a preference about which account to burn NEXT, and
        it sat ahead of the escape arm for every trigger but `at-limit`. Once
        the spent guard began admitting candidates on a RECOVERY argument,
        `disabled-active` -- the one trigger that does so without reaching the
        tiered key -- landed here instead, ranked on a weekly reset those
        candidates were never selected for, and took the peer that lifts LAST.

        Its control is `test_a_disabled_active_lands_on_the_peer_that_lifts_first`:
        the same fleet on the default strategy, which chose correctly all along.
        """
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0,
                          strategy="consume-first")
        for num, email in enumerate(("a", "b", "c"), 1):
            h.seed(num, f"{email}@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        # THE WEEKLY RESETS ARE THE POINT. Without them `reset_ts` is None for
        # every slot, the old key degenerates to `(inf, -0.0)`, and the wrong
        # answer comes from slot order rather than from the weekly axis -- a
        # fixture that a "add a recovery tiebreak to the weekly key" fix would
        # satisfy with the defect fully intact. Slot 2 holds the soonest
        # weekly, so the old code picks it ON THAT AXIS while it is the peer
        # that lifts last.
        weeklies = {1: 3 * 86400, 2: 1 * 3600, 3: 6 * 86400}
        usage = {}
        for num, mins in enumerate((60, 50, 10), 1):
            row = _usage7(100.0, 60.0, _iso_at(now + weeklies[num]))
            row["five_hour"]["resets_at"] = _iso_at(now + mins * 60)
            usage[str(num)] = row
        h.switcher.set_account_disabled("1", True)
        outcome = h.tick_with_usage(usage)
        assert h.active_number() == 3, (
            f"consume-first landed on {h.active_number()} ({outcome}): slot 3 "
            "lifts in ten minutes and slot 2 in fifty, and the weekly reset "
            "is not why either of them was admitted"
        )

    def test_a_disabled_active_still_honours_consume_first_when_peers_are_healthy(
        self, temp_home
    ):
        """THE OTHER HALF, and the one an over-broad fix quietly costs.

        The spent guard admits on a recovery argument only under `all_above`,
        so that is the state where a candidate can be ranked on an axis it was
        not selected for. Below the threshold a disabled active's peers are
        just healthy accounts, and burning the most perishable weekly quota
        first is exactly what `--strategy consume-first` asks for.

        Excluding the TRIGGER rather than the STATE passes every other test in
        this file while silently dropping that.
        """
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0,
                          strategy="consume-first")
        for num, email in enumerate(("a", "b", "c"), 1):
            h.seed(num, f"{email}@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        h.switcher.set_account_disabled("1", True)
        usage = {
            "1": _usage7(50.0, 50.0, _iso_at(now + 3 * 86400)),
            "2": _usage7(60.0, 60.0, _iso_at(now + 1 * 3600)),   # 40 pts, weekly in 1h
            "3": _usage7(30.0, 30.0, _iso_at(now + 6 * 86400)),  # 70 pts, weekly in 6d
        }
        outcome = h.tick_with_usage(usage)
        assert h.active_number() == 2, (
            f"landed on {h.active_number()} ({outcome}): slot 2 holds 40 "
            "points whose weekly window perishes in an hour and slot 3 holds "
            "70 with six days left -- consume-first exists to burn the first"
        )

    def test_a_near_limit_peer_does_not_win_the_weekly_arm(self, temp_home):
        """A soonest weekly reset is not a reason to land somewhere unusable.

        `disabled-active` skips the landing-health gate -- every escape does --
        so this arm is the one place a candidate reaches a ranking with no
        admission axis at all. One below-threshold peer is enough to make
        `all_above` False, and the weekly key then hands the tick to whichever
        account's weekly perishes soonest, however little it can serve.
        """
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0,
                          strategy="consume-first")
        for num, email in enumerate(("a", "b", "c"), 1):
            h.seed(num, f"{email}@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        h.switcher.set_account_disabled("1", True)
        outcome = h.tick_with_usage({
            "1": _usage7(95.0, 95.0, _iso_at(now + 3 * 86400)),   # 5 pts
            "2": _usage7(98.0, 98.0, _iso_at(now + 1 * 3600)),    # 2 pts, weekly in 1h
            "3": _usage7(10.0, 10.0, _iso_at(now + 6 * 86400)),   # 90 pts, weekly in 6d
        })
        assert h.active_number() == 3, (
            f"landed on {h.active_number()} ({outcome}): slot 2 holds two "
            "points and slot 3 ninety -- a weekly window perishing in an hour "
            "is worth nothing on an account that cannot serve the tick"
        )

    def test_failover_does_not_escape_onto_a_near_limit_peer(self, temp_home):
        """The same arm, the other trigger that skips every gate.

        `all_above` is always False under failover -- the active is unreadable,
        so `_every_account_above_threshold` refuses -- which is why the state
        condition above cannot reach this. The landing tier can.
        """
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0,
                          strategy="consume-first")
        for num, email in enumerate(("a", "b", "c"), 1):
            h.seed(num, f"{email}@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        usage = {
            "1": None,                                            # unreadable
            "2": _usage7(98.0, 98.0, _iso_at(now + 1 * 3600)),     # 2 pts
            "3": _usage7(10.0, 10.0, _iso_at(now + 6 * 86400)),    # 90 pts
        }
        for _ in range(2):
            assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        outcome = h.tick_with_usage(usage)
        assert h.active_number() == 3, (
            f"landed on {h.active_number()} ({outcome}): the active is dead "
            "and this escape took the two-point account because its weekly "
            "perishes soonest"
        )

    def test_the_escape_does_not_land_on_a_peer_that_walls_immediately(
        self, temp_home
    ):
        """The escape axis orders; it does not decide usability.

        `headroom_on_window` is a ranking among accounts "already known
        usable", and the bar for usable was one point of headroom. A peer with
        fifty points on the very window that blocked us and ONE point on its
        weekly outranks a peer holding forty on both -- then walls on the next
        request, and the following tick pays a second credential swap to
        correct it.
        """
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0)
        for num, email in enumerate(("a", "b", "c"), 1):
            h.seed(num, f"{email}@example.com")
        h.make_live("a@example.com", 1)
        outcome = h.tick_with_usage({
            "1": _usage7(100.0, 50.0),   # at-limit on the 5h window
            "2": _usage7(50.0, 99.0),    # 50 on the escape axis, ONE point real
            "3": _usage7(60.0, 60.0),    # 40 points on both
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3, (
            f"landed on {h.active_number()} ({outcome}): slot 2 looks best on "
            "the blocked window and holds one point of actual headroom, so "
            "the escape lands somewhere that stops answering immediately"
        )

    def test_a_peer_exactly_at_the_threshold_is_not_a_healthy_landing(
        self, temp_home
    ):
        """The `<` in the landing tier, which nothing else holds.

        The tier has to be the landing gate's complement, and `<=` would put an
        account sitting EXACTLY at the threshold — the one the landing gate
        rejects — in the healthy tier. `pct` is a float straight from the API
        against a round default, so exact equality is ordinary, not a knife
        edge, and the whole suite stays green when the two drift apart.
        """
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0,
                          strategy="consume-first")
        for num, email in enumerate(("a", "b", "c"), 1):
            h.seed(num, f"{email}@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        h.switcher.set_account_disabled("1", True)
        outcome = h.tick_with_usage({
            "1": _usage7(95.0, 95.0, _iso_at(now + 3 * 86400)),
            "2": _usage7(90.0, 90.0, _iso_at(now + 1 * 3600)),   # EXACTLY at it
            "3": _usage7(10.0, 10.0, _iso_at(now + 6 * 86400)),
        })
        assert h.active_number() == 3, (
            f"landed on {h.active_number()} ({outcome}): slot 2 sits exactly "
            "at the threshold, which the landing gate calls unhealthy, so its "
            "sooner weekly must not outrank a peer with ninety points"
        )

    def test_a_perishing_weekly_does_not_beat_being_able_to_serve(
        self, temp_home
    ):
        """Tier 1 of the weekly key, where the escapes have no admission axis.

        Both peers are over the threshold, so neither is a healthy landing and
        the tier cannot separate them -- but two points is under two poll
        intervals of work by this module's own line, and nine "really is
        somewhere to work". A weekly window perishing in an hour is worth
        nothing on the account that cannot spend it.
        """
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0,
                          strategy="consume-first")
        for num, email in enumerate(("a", "b", "c"), 1):
            h.seed(num, f"{email}@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        h.switcher.set_account_disabled("1", True)
        outcome = h.tick_with_usage({
            "1": _usage7(80.0, 80.0, _iso_at(now + 3 * 86400)),   # 20 pts
            "2": _usage7(98.0, 98.0, _iso_at(now + 1 * 3600)),    # 2 pts, weekly 1h
            "3": _usage7(91.0, 91.0, _iso_at(now + 6 * 86400)),   # 9 pts, weekly 6d
        })
        assert h.active_number() == 3, (
            f"landed on {h.active_number()} ({outcome}): slot 2 holds two "
            "points, below the bar this module calls spent, and slot 3 nine"
        )

    def test_a_high_threshold_does_not_make_a_spent_account_a_landing(
        self, temp_home
    ):
        """Servability and landing health are DIFFERENT bars, and above 97
        they disagree.

        "Healthy landing" is `h > 100 - threshold`, and the threshold is the
        user's to set anywhere up to 99.9 -- so above 97 the landing gate
        calls a spent account legal, and asking health first hands the weekly
        to an account with no room to spend it. Ordering the two the other way
        is identical everywhere below 97, where healthy already implies
        servable, so it only ever decides the case the gate gets wrong.
        """
        h = EngineHarness(temp_home, threshold=99.0, hysteresis_pct=1.0,
                          strategy="consume-first")
        for num, email in enumerate(("a", "b", "c"), 1):
            h.seed(num, f"{email}@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        outcome = h.tick_with_usage({
            "1": _usage7(99.5, 99.5, _iso_at(now + 5 * 86400)),   # 0.5 pt
            "2": _usage7(97.1, 97.1, _iso_at(now + 1 * 3600)),    # 2.9 pts, weekly 1h
            "3": _usage7(31.0, 31.0, _iso_at(now + 6 * 86400)),   # 69 pts, weekly 6d
        })
        assert h.active_number() == 3, (
            f"landed on {h.active_number()} ({outcome}): slot 2 clears a "
            "threshold of 99 with 2.9 points, which is under the bar this "
            "module calls spent -- it cannot spend the weekly it is being "
            "chosen for, while slot 3 holds sixty-nine points"
        )

    def test_a_spent_peer_is_refused_even_when_the_servable_one_fails(
        self, temp_home
    ):
        """The spent bar decides ADMISSION, and the ranking cannot stand in.

        Its sibling above pins the same conjunct only through the head of the
        list: the escape key's servability tier sorts the spent peer last, so
        the winner is the same with the bar deleted and the case stops
        discriminating. `_tick_inner` iterates the WHOLE list, so a candidate
        the bar should have excluded is reachable the moment the peer ahead of
        it cannot be freshened -- and it is at 100% on its five-hour window.

        This is what a later fix subsuming an earlier one's mechanism looks
        like from the outside: a green suite and a guard nothing kills.
        """
        h = EngineHarness(temp_home, threshold=90.0, hysteresis_pct=5.0)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        now = h.clock.now
        active = _usage7(50.0, 100.0, _iso_at(now + 3 * 86400))
        spent = _usage7(100.0, 50.0)
        spent["five_hour"]["resets_at"] = _iso_at(now + 10 * 60)
        servable = _usage7(95.0, 95.0)
        h.engine._freshen_target = (
            lambda num, email: "transient" if num == "3" else "ok"
        )
        outcome = h.tick_with_usage(
            {"1": active, "2": spent, "3": servable}
        )
        assert outcome is TickOutcome.ERROR, (
            f"expected the tail to be reached and refused, got {outcome}: "
            "staying put also happens when the ranking is EMPTY, and an empty "
            "ranking never exercises the bar this case exists for"
        )
        assert h.active_number() == 1, (
            f"landed on {h.active_number()} ({outcome}): slot 3 could not be "
            "freshened this tick, and the engine fell through to slot 2, "
            "whose five-hour window is at 100% and can answer nothing"
        )

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
        h = EngineHarness(temp_home, cooldown_seconds=0.0, strategy="best")
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
        # A PEER HOLDS THE EARLIEST, or this cannot tell the announcement from
        # the active's own reset. It stays BLOCKED because that peer is only
        # three minutes sooner: taking the wall on the account that lifts first
        # needs RECOVERY_HYSTERESIS_S of daylight, and three minutes is inside
        # it. Both facts are load-bearing — with the active earliest, a mutant
        # announcing "the first blocked account" survives.
        outcome = harness.tick_with_usage({
            "1": _usage(100, "2026-07-03T11:00:00Z"),
            "2": _usage(100, "2026-07-03T10:57:00Z"),
            "3": _usage(100, "2026-07-03T12:00:00Z"),
        })
        assert outcome is TickOutcome.BLOCKED
        event = next(e for e in harness.events if isinstance(e, AllExhaustedEvent))
        assert event.earliest_reset_at == "2026-07-03T10:57:00Z"
        assert harness.engine._sleep_until_ts is not None
        # The other arm of `deliberate_wait`. Every peer here is at its limit,
        # so this IS the exhausted fleet -- and nothing else in the suite reads
        # the flag as False, which would let a real exhaustion be relabelled.
        assert event.deliberate_wait is False
        assert "all accounts exhausted" in event.human()

    def test_a_reset_already_past_is_not_provable_either(self, harness):
        """The `usable_at <= now` half, which nothing reads the flag for.

        Its sibling below puts the SAME past reset on all three accounts, so
        `earliest` is None whatever the flag says and the value is never
        consulted. Mixed -- one account already past, the others hours out --
        the two halves separate: a past reset means that account could return
        at any moment, so the fleet is no more provable than one with no reset
        at all, and announcing the next account's is a claim over it.
        """
        from datetime import datetime, timezone

        def _at(offset):
            return (
                datetime.fromtimestamp(harness.clock.now + offset, tz=timezone.utc)
                .isoformat().replace("+00:00", "Z")
            )

        outcome = harness.tick_with_usage({
            "1": _usage(100, _at(-60)),
            "2": _usage(100, _at(2 * 3600)),
            "3": _usage(100, _at(3 * 3600)),
        })
        assert outcome is TickOutcome.BLOCKED
        event = next(e for e in harness.events if isinstance(e, AllExhaustedEvent))
        assert event.earliest_reset_at is None, (
            f"announced {event.earliest_reset_at!r} while account 1's reset has "
            "already passed — it can return at any moment and nothing measured it"
        )
        assert harness.engine._sleep_until_ts is None, (
            "the sleep armed toward a later account's reset over one that is "
            "already due"
        )
        assert harness.engine._next_delay(outcome) == NO_RESET_FALLBACK_S

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


class TestProactiveExcludesStaleUsage:
    """The `proactive` trigger (strategy `best`, above threshold) must refuse
    a candidate the same way `consume-first`'s phase-2 refetch already does:
    a cached usage row that is stale/unreadable (active backoff, a run of
    poll failures, a failure `lastError`) is never admitted as a landing
    spot, even when its `last_good` figures rank it best. Without this, a
    candidate walled off by the collector (repeated 429s) gets consumed as
    if its cached headroom were live."""

    def _harness(self, temp_home: Path) -> EngineHarness:
        h = EngineHarness(temp_home, strategy="best")
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        return h

    def test_proactive_does_not_admit_a_backed_off_candidate(self, temp_home):
        """The Account-6 shape: `lastGood` genuinely too old (past even the
        collector's own extended trust, not merely `fresh()`'s TTL —
        `trust_extended=False`, unlike the decision-trusted backoff a peer
        stays a target on, see `test_proactive_admits_a_throttled_but_fresh_
        candidate`), a run of failures and an active backoff. `decision_
        value()` no longer trusts this reading -- and BEFORE
        `candidate_is_untrustworthy` is ever consulted, ranking's own
        headroom-None filter has already dropped it (`usage[num] =
        entry.decision_value()`, `headroom.get(num) is None: continue` in
        `_rank_candidates_pass`) -- so the tick reads "no candidate has
        readable usage", not the per-candidate skip. Either way the
        candidate is never landed on."""
        from claude_swap.autoswitch import candidate_is_untrustworthy

        h = self._harness(temp_home)
        # Active over threshold (headroom 5 < hysteresis floor) -> must move.
        active_entry = _entry_for(_usage(95), h.clock.now)
        # #2's cached figures (headroom 100) would rank it best, but the
        # entry is exactly what a walled, failing candidate looks like: old
        # `fetched_at` well past decision trust, repeated failures, an
        # active backoff and a failure `lastError`.
        stale_entry = UsageEntry(
            last_good=_usage(0),
            fetched_at=h.clock.now - 7200.0,
            age_s=7200.0,
            consecutive_failures=9,
            last_error="http-429",
            backoff_until=h.clock.now + 400.0,
        )
        assert stale_entry.decision_value() is None
        assert candidate_is_untrustworthy(stale_entry, h.clock.now)
        outcome = h.tick_with_entries({"1": active_entry, "2": stale_entry})
        # No OTHER candidate in this 2-account fleet, and #2's usage is
        # unreadable (decision-untrusted) -> BLOCKED ("wanted to switch but
        # no viable target"), not NO_ACTION.
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert "no-comparison" in reasons

    def test_control_the_same_candidate_fresh_is_admitted(self, temp_home):
        """Mutant control: the same candidate, freshly fetched, IS switched
        to — proving #2 was otherwise rank-eligible and the hold above is the
        staleness gate firing, not some other refusal."""
        h = self._harness(temp_home)
        active_entry = _entry_for(_usage(95), h.clock.now)
        fresh_entry = _entry_for(_usage(0), h.clock.now)
        outcome = h.tick_with_entries({"1": active_entry, "2": fresh_entry})
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "proactive"

    def test_proactive_admits_a_stale_but_decision_trusted_candidate(
        self, temp_home
    ):
        """m-1: past `fresh()`'s SERVE_TTL_S (180 s) is not "cannot be
        trusted" on its own -- `decision_value()` already trusts this row
        (well inside STALE_OK_S, 300 s), and nothing about it carries the
        incident's own signature (no backoff, no failures, no strike). The
        full staleness predicate refused it; `proactive`'s own bar must
        admit it."""
        h = self._harness(temp_home)
        active_entry = _entry_for(_usage(95), h.clock.now)
        trusted_stale = UsageEntry(
            last_good=_usage(0), fetched_at=h.clock.now - 200.0, age_s=200.0,
        )
        outcome = h.tick_with_entries({"1": active_entry, "2": trusted_stale})
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "proactive"

    def test_control_the_same_age_with_a_bare_failure_is_still_admitted(
        self, temp_home
    ):
        """Mutant control: identical age (200s, inside STALE_OK_S), and a
        recorded failure but no backoff -- a bare failure count is not the
        incident's signature on its own; `decision_value()` still trusts
        this reading, so it stays admitted, same as the age-alone case
        above. Only backoff/failures COMBINED with a reading `decision_
        value()` no longer trusts refuses (see
        `test_proactive_does_not_admit_a_backed_off_candidate`)."""
        h = self._harness(temp_home)
        active_entry = _entry_for(_usage(95), h.clock.now)
        failing_stale = UsageEntry(
            last_good=_usage(0), fetched_at=h.clock.now - 200.0, age_s=200.0,
            consecutive_failures=3,
        )
        assert failing_stale.decision_value() is not None
        outcome = h.tick_with_entries({"1": active_entry, "2": failing_stale})
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "proactive"

    def test_proactive_does_not_admit_a_struck_candidate_even_when_fresh(
        self, temp_home
    ):
        """A collector-struck (`invalid_grant`, twice — past the race-doubt
        window) candidate must not be admitted even in the narrow window
        where its `fetched_at` still reads fresh (the strike landed without
        a later success reaging it) — the freshness gate alone (previous
        test) would pass this one through."""
        h = self._harness(temp_home)
        active_entry = _entry_for(_usage(95), h.clock.now)
        struck_entry = UsageEntry(
            last_good=_usage(0),
            fetched_at=h.clock.now,
            age_s=0.0,
            auth_dead_strikes=2,
        )
        assert struck_entry.token_dead()
        outcome = h.tick_with_entries({"1": active_entry, "2": struck_entry})
        # See test_proactive_does_not_admit_a_backed_off_candidate: skipping
        # the only candidate falls through to BLOCKED, not NO_ACTION.
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert "stale-candidate-skipped" in reasons

    def test_proactive_admits_a_throttled_but_fresh_candidate(self, temp_home):
        """The reviewer's own fixture: `fetched_at` moves only on success,
        `backoffUntil`/`consecutiveFailures` only on failure, so "succeeded
        30s ago, then one poll 429'd" is fresh (well inside `STALE_OK_S`,
        300s) and decision-trusted — `in_backoff`/`consecutive_failures`
        alone is not the incident's signature; only backoff/failures PLUS a
        reading `decision_value()` no longer trusts is. Today (before the
        fix) this candidate is refused for the whole backoff and the tick
        falls through to BLOCKED — see the mutant below, which reproduces
        that by dropping the reading-stale half of the predicate."""
        h = self._harness(temp_home)
        active_entry = _entry_for(_usage(95), h.clock.now)
        throttled_but_fresh = UsageEntry(
            last_good=_usage(0),
            fetched_at=h.clock.now - 30.0,
            age_s=30.0,
            consecutive_failures=1,
            last_error="http-429",
            backoff_until=h.clock.now + 3600.0,
        )
        outcome = h.tick_with_entries({"1": active_entry, "2": throttled_but_fresh})
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "proactive"

    def test_candidate_is_untrustworthy_construction(self):
        """The predicate itself, on the five cases the fix must classify
        correctly (`_rank_candidates_pass`'s own headroom-None filter
        already keeps a decision-untrusted candidate out of the engine's
        per-candidate loop -- see `test_proactive_does_not_admit_a_backed_
        off_candidate` -- so this checks the FUNCTION directly rather than
        only through a reachable tick)."""
        from claude_swap.autoswitch import candidate_is_untrustworthy

        now = 1_000_000.0
        # 1. API-key last-resort sentinel: never fetched, no failures.
        sentinel = UsageEntry(sentinel="api-key")
        assert not candidate_is_untrustworthy(sentinel, now)
        # 2. A claimed/still-fresh-enough row, no failures.
        claimed = UsageEntry(
            last_good=_usage(0), fetched_at=now - 200.0, age_s=200.0,
        )
        assert not candidate_is_untrustworthy(claimed, now)
        # 3. Throttled but fresh: the reviewer's own shape.
        throttled_but_fresh = UsageEntry(
            last_good=_usage(0),
            fetched_at=now - 30.0,
            age_s=30.0,
            consecutive_failures=1,
            last_error="http-429",
            backoff_until=now + 3600.0,
        )
        assert not candidate_is_untrustworthy(throttled_but_fresh, now)
        # 4. Account-6 shape: genuinely too old for decision_value() to
        # trust, on top of the failures/backoff.
        account_6 = UsageEntry(
            last_good=_usage(0),
            fetched_at=now - 7200.0,
            age_s=7200.0,
            consecutive_failures=9,
            last_error="http-429",
            backoff_until=now + 400.0,
        )
        assert candidate_is_untrustworthy(account_6, now)
        # 5. Struck, even while fresh.
        struck = UsageEntry(
            last_good=_usage(0), fetched_at=now, age_s=0.0, auth_dead_strikes=2,
        )
        assert candidate_is_untrustworthy(struck, now)


class TestProactiveSkipsAStaleTopCandidateForTheNextRanked:
    """A stale top-ranked candidate must not park the WHOLE tick: `proactive`
    (unlike consume-first, see below) tries the next-ranked healthy
    candidate instead of holding — the fleet-exhaustion `best`'s own
    two-candidate fixture above cannot see (the only candidate stale IS the
    whole fleet exhausted; nothing is left to prefer)."""

    def _harness(self, temp_home: Path) -> EngineHarness:
        h = EngineHarness(temp_home, strategy="best")
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        return h

    def test_switches_to_the_healthy_next_ranked_candidate(self, temp_home):
        from claude_swap.autoswitch import candidate_is_untrustworthy

        h = self._harness(temp_home)
        # Active over threshold, real headroom (5) -> proactive, not at-limit.
        active_entry = _entry_for(_usage(95), h.clock.now)
        # #2 would rank best (headroom 100) but is walled by the collector,
        # and genuinely too old for `decision_value()` to trust any more
        # (unlike a decision-trusted backoff, which stays a target — see
        # TestProactiveExcludesStaleUsage.test_proactive_admits_a_throttled_
        # but_fresh_candidate).
        stale_top = UsageEntry(
            last_good=_usage(0),
            fetched_at=h.clock.now - 7200.0,
            age_s=7200.0,
            consecutive_failures=9,
            last_error="http-429",
            backoff_until=h.clock.now + 400.0,
        )
        assert stale_top.decision_value() is None
        assert candidate_is_untrustworthy(stale_top, h.clock.now)
        # #3 ranks second (headroom 90) and is healthy — the fix's target.
        healthy_next = _entry_for(_usage(10), h.clock.now)
        outcome = h.tick_with_entries(
            {"1": active_entry, "2": stale_top, "3": healthy_next}
        )
        assert outcome is TickOutcome.SWITCHED
        # The FAULT this guards: landing on the stale top candidate instead
        # of skipping to the healthy one — assert the marker directly, not
        # only the outcome. #2's unreadable usage drops it out of ranking
        # itself (headroom-None), so no per-candidate skip event fires here
        # — see test_proactive_does_not_admit_a_backed_off_candidate.
        assert h.active_number() == 3
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "proactive"

    def test_control_the_same_top_candidate_fresh_is_taken_instead(
        self, temp_home
    ):
        """Mutant control: #2 fresh (not stale) outranks #3 and IS taken —
        proving the skip above is the staleness gate choosing #3, not #2
        being unrankable for some other reason."""
        h = self._harness(temp_home)
        active_entry = _entry_for(_usage(95), h.clock.now)
        fresh_top = _entry_for(_usage(0), h.clock.now)
        healthy_next = _entry_for(_usage(10), h.clock.now)
        outcome = h.tick_with_entries(
            {"1": active_entry, "2": fresh_top, "3": healthy_next}
        )
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "proactive"


class TestFailoverSkipsAStaleTopCandidate:
    """m1: `failover` (active usage unreadable) reaches the same per-
    candidate freshen loop as `proactive` — a struck/token-dead top-ranked
    candidate must be skipped for a healthy next-ranked one, never landed
    on via `_freshen_target`'s near-expiry fast path (which returns "ok"
    without ever reading the collector's stale/struck verdict on this
    candidate — see `_freshen_target`'s own docstring)."""

    def _harness(self, temp_home: Path) -> EngineHarness:
        h = EngineHarness(temp_home, strategy="best")
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        return h

    def _drive_to_failover(self, h, entries_after):
        """`unhealthy_ticks` (3, default) consecutive unreadable-active
        ticks are spent before failover is even considered."""
        blank = {"1": UsageEntry(), "2": UsageEntry(), "3": UsageEntry()}
        for _ in range(h.settings.unhealthy_ticks - 1):
            outcome = h.tick_with_entries(blank)
            assert outcome is TickOutcome.NO_ACTION
        return h.tick_with_entries(entries_after)

    def test_skips_a_token_dead_top_candidate(self, temp_home):
        h = self._harness(temp_home)
        struck_top = UsageEntry(
            last_good=_usage(0), fetched_at=h.clock.now, age_s=0.0,
            auth_dead_strikes=2,
        )
        assert struck_top.token_dead()
        healthy_next = _entry_for(_usage(10), h.clock.now)
        outcome = self._drive_to_failover(
            h, {"1": UsageEntry(), "2": struck_top, "3": healthy_next}
        )
        assert outcome is TickOutcome.SWITCHED
        # The FAULT this guards: failover landing on the dead top candidate.
        assert h.active_number() == 3
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "failover"

    def test_control_the_same_top_candidate_healthy_is_taken_instead(
        self, temp_home
    ):
        h = self._harness(temp_home)
        healthy_top = _entry_for(_usage(0), h.clock.now)
        other = _entry_for(_usage(10), h.clock.now)
        outcome = self._drive_to_failover(
            h, {"1": UsageEntry(), "2": healthy_top, "3": other}
        )
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "failover"

    @staticmethod
    def _backed_off(usage, now):
        """A peer in a 429 backoff: past `fresh()`'s TTL, still decision-
        trusted (`trust_extended`), credential fine."""
        return UsageEntry(
            last_good=usage, fetched_at=now - 1000.0, age_s=1000.0,
            consecutive_failures=9, last_error="http-429",
            backoff_until=now + 400.0, trust_extended=True,
        )

    def test_lands_on_a_backed_off_peer_when_the_active_is_struck(
        self, temp_home
    ):
        """I-a: failover is the ACTIVE credential failing. A peer in a 429
        backoff is trusted-but-not-fresh by design (usage_store keeps
        serving a throttled row so it stays a switch target); gating
        failover on the full staleness predicate refused every such peer
        and left the engine BLOCKED on a fleet whose credentials were all
        fine. Only a struck credential may disqualify a failover target."""
        h = self._harness(temp_home)
        outcome = self._drive_to_failover(
            h,
            {
                "1": UsageEntry(),
                "2": self._backed_off(_usage(0), h.clock.now),
                "3": self._backed_off(_usage(10), h.clock.now),
            },
        )
        assert outcome is TickOutcome.SWITCHED, outcome
        assert h.active_number() == 2
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "failover"

    def test_control_a_struck_top_peer_is_still_skipped_for_a_backed_off_one(
        self, temp_home
    ):
        h = self._harness(temp_home)
        struck_top = UsageEntry(
            last_good=_usage(0), fetched_at=h.clock.now, age_s=0.0,
            auth_dead_strikes=2,
        )
        assert struck_top.token_dead()
        outcome = self._drive_to_failover(
            h,
            {
                "1": UsageEntry(),
                "2": struck_top,
                "3": self._backed_off(_usage(10), h.clock.now),
            },
        )
        assert outcome is TickOutcome.SWITCHED, outcome
        assert h.active_number() == 3
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert "stale-candidate-skipped" in reasons


_PR_321_BASE_SHA = "f227dffb76b1086b05c3e35ba07f275bbc9a41a1"


def _base_engine_results(
    tmp_path: Path, strategy: str, seed: int, n_fleets: int,
    *, base_sha: str = _PR_321_BASE_SHA, custom_fleets: list[dict] | None = None,
    settings_kwargs: dict | None = None,
):
    """`n_fleets` fleets generated from `seed` (the same generator
    `TestOutcomeDigestAgainstBase._fleet` uses) — or, when `custom_fleets`
    is given, those NAMED fleets verbatim instead — run through the
    `base_sha` `AutoSwitchEngine` in a fresh subprocess against ITS OWN
    `claude_swap` package — a different `usage_store`/`switcher`/`settings`/
    `autoswitch`, not merely a fresh module namespace sharing this process's
    `sys.modules`. Same technique as `test_dynamic_isolation.py`'s
    `_digests_at_base_rev`: guard-and-skip, `git archive` + `tarfile`, a
    driver subprocess, a positive control on where `claude_swap` resolved
    from.

    SKIPPED, not failed, when `base_sha` is not in the object database —
    CI checks out at `refs/pull/N/merge` with fetch-depth 1, so this
    branch's own pre-PR base is never in that checkout, and a bare
    `git show` there would redden every job today and every run forever
    once the branch is deleted post-merge.
    """
    repo_root = Path(__file__).resolve().parents[1]
    probe = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{base_sha}^{{commit}}"],
    )
    if probe.returncode != 0:
        pytest.skip(f"{base_sha} is not in this checkout's object database")
    old_root = tmp_path / "base_src"
    old_root.mkdir()
    archive = subprocess.run(
        ["git", "-C", str(repo_root), "archive", "--format=tar", base_sha, "src"],
        capture_output=True, check=True,
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tf:
        tf.extractall(old_root, filter="data")  # trusted: our own repo's history
    module_dir = tmp_path / "base_driver"
    module_dir.mkdir()
    # HEAD's test module runs against BASE's package: a name this file imports
    # from `claude_swap` that post-dates the base sha reddens the driver
    # (ImportError) instead of skipping.
    (module_dir / "test_autoswitch.py").write_text(
        Path(__file__).read_text(encoding="utf-8"), encoding="utf-8"
    )
    homes_dir = tmp_path / f"base_homes_{strategy}"
    homes_dir.mkdir()
    driver = module_dir / "_zz_base_engine_driver.py"
    if custom_fleets is not None:
        # `custom_fleets`: raw {account: usage-dict} maps (JSON-friendly);
        # `_run` wants {account: UsageEntry} same as `_fleet` produces, so
        # the driver converts via `_entry_for` at the SAME `now` `_fleet`
        # anchors on.
        fleets_dir = tmp_path / "custom_fleets"
        fleets_dir.mkdir()
        fleets_file = fleets_dir / "fleets.json"
        fleets_file.write_text(json.dumps(custom_fleets))
        fleets_setup = (
            f"raw_fleets = json.loads(Path({str(fleets_file)!r}).read_text())\n"
            "fleets = [\n"
            "    {num: ta._entry_for(v, 1_000_000.0) for num, v in raw.items()}\n"
            "    for raw in raw_fleets\n"
            "]\n"
        )
    else:
        fleets_setup = (
            f"rng = random.Random({seed})\n"
            "fleets = []\n"
            f"for _ in range({n_fleets}):\n"
            "    entries, _ = ta.TestOutcomeDigestAgainstBase._fleet(None, rng, 1_000_000.0)\n"
            "    fleets.append(entries)\n"
        )
    driver.write_text(
        "import sys, json, random\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(old_root / 'src')!r})\n"
        f"sys.path.insert(0, {str(module_dir)!r})\n"
        "import claude_swap\n"
        "import test_autoswitch as ta\n"
        f"{fleets_setup}"
        "results = ta.TestOutcomeDigestAgainstBase._run(\n"
        f"    None, ta.AutoSwitchEngine, Path(sys.argv[1]), 'base', fleets, {strategy!r},\n"
        f"    **{settings_kwargs or {}!r}\n"
        ")\n"
        "print(json.dumps({'_claude_swap_file': claude_swap.__file__, "
        "'results': results}))\n"
    )
    result = subprocess.run(
        [sys.executable, str(driver), str(homes_dir)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # The digest is a PR-branch invariant (base vs THIS PR's head), not
        # an integration one: where a later PR's merge has added a symbol
        # to `claude_swap.autoswitch` that this test module now imports,
        # base's own package (frozen at `base_sha`) cannot satisfy it and
        # the comparison is undefined — SKIP, exactly as this function
        # already skips when the base object itself is absent. Any OTHER
        # subprocess failure (a real digest-run crash) still reds.
        import_line = next(
            (
                line for line in result.stderr.splitlines()
                if "ImportError" in line or "ModuleNotFoundError" in line
            ),
            None,
        )
        if import_line is not None:
            pytest.skip(
                f"base {base_sha[:8]} cannot import this test "
                f"module: {import_line}"
            )
    assert result.returncode == 0, (
        f"base-engine driver failed: rc={result.returncode}\n"
        f"STDOUT={result.stdout}\nSTDERR={result.stderr}"
    )
    out = json.loads(result.stdout.strip().splitlines()[-1])
    # POSITIVE CONTROL, per test_dynamic_isolation.py: an editable install
    # could resolve `claude_swap` to the working tree regardless of
    # `sys.path` order, which would compare HEAD against itself.
    assert out["_claude_swap_file"].startswith(str(old_root)), (
        f"the subprocess imported claude_swap from outside {old_root} — "
        f"this compared HEAD against itself, not against {base_sha}"
    )
    return [
        (name, active, tuple(tuple(e) for e in events))
        for name, active, events in out["results"]
    ]


class TestOutcomeDigestAgainstBase:
    """The #321 invariant: `best`'s ranking/admission must stay identical to
    the PR's base commit except the one exclusion this PR adds. Proven by
    running identical random fleets through the pre-fix and post-fix
    `AutoSwitchEngine` and diffing the whole per-tick outcome sequence —
    with a mutant control that reproduces the pre-fix digest exactly by
    neutralizing only the new exclusion, so the delta is attributable to
    that one function and nothing else moved.
    """

    # EIGHT, not forty (#414, the owner's suite-time bar). Every control in
    # this class and in `TestOutcomeDigest375` still holds on the first eight
    # fleets of this seed, MEASURED rather than assumed: head/base diverge on
    # fleet 1 for `best` (5 of 40 at the old count) and on fleets 0/1/4/7 for
    # `consume-first` (6 of 40), and the stale-exclusion mutant still diverges
    # from base on fleet 6 (4 of 40) and still moves the head digest for every
    # strategy. Those are exactly what `assert diffs`, `assert mutant_diffs`
    # and `assert mutant_results != head_results` below check, so a seed or a
    # ranking change that empties them fails loudly instead of passing
    # vacuously -- raise this number then, and say what stopped being caught.
    # Cost: the two digest classes went 12.1s -> 4.8s run alone.
    _N_FLEETS = 8
    _DIGEST_SEED = 20260321

    def _fleet(self, rng: random.Random, now: float):
        resets = (_R_SOON, _R_LATER, _R_LATEST)   # module globals, late-bound
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

    def _run(
        self,
        engine_cls,
        tmp_path: Path,
        tag: str,
        fleets: list[dict],
        strategy: str,
        **settings_kwargs,
    ):
        # `EngineHarness.__init__` only patches `Path.home()` for its own
        # setup — every OTHER test in this file relies on the `temp_home`
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

    def _head(self, tmp_path, strategy, **settings_kwargs):
        rng = random.Random(self._DIGEST_SEED)
        now = 1_000_000.0
        fleets: list[dict] = []
        stale_by_fleet: list[set] = []
        for _ in range(self._N_FLEETS):
            entries, stale_nums = self._fleet(rng, now)
            fleets.append(entries)
            stale_by_fleet.append(stale_nums)
        head_results = self._run(
            AutoSwitchEngine, tmp_path, "head", fleets, strategy, **settings_kwargs
        )
        return fleets, stale_by_fleet, head_results

    def _mutant(self, tmp_path, fleets, strategy, **settings_kwargs):
        # Neutralize ONLY the exclusions the admission gate can reach (the
        # gate always reads "not stale"/"not untrustworthy") on the SAME
        # fleets. `trigger == "proactive"` is reachable under ANY strategy
        # setting (utilization over threshold with real headroom, regardless
        # of `settings.strategy`), so its own predicate needs neutralizing
        # too, not only the consume-first-literal HOLD's.
        with (
            patch(
                "claude_swap.autoswitch.candidate_usage_is_stale",
                return_value=False,
            ),
            patch(
                "claude_swap.autoswitch.candidate_is_untrustworthy",
                return_value=False,
            ),
        ):
            return self._run(
                AutoSwitchEngine, tmp_path, "mutant", fleets, strategy, **settings_kwargs
            )

    @pytest.mark.parametrize("strategy", ["best", "consume-first", "dynamic"])
    def test_mutant_moves_the_head_digest(self, tmp_path, strategy):
        """Needs no base checkout, so it runs on CI's shallow clone too."""
        fleets, _stale, head_results = self._head(tmp_path, strategy)
        mutant_results = self._mutant(tmp_path, fleets, strategy)
        assert mutant_results != head_results, (
            "the mutant must move the digest — a no-op injection proves nothing"
        )

    # `dynamic` excluded here (#375): this checks base ``_PR_321_BASE_SHA``
    # (PR #321's OWN base commit) against head, on the invariant "nothing
    # but the stale-candidate exclusion moved" — true when #321 was the
    # only round touching `dynamic` since that base. #375 is a further,
    # separately-authorized round narrowing `dynamic`'s proactive trigger
    # to `about_to_wall` and adding alternation, so `dynamic` now diverges
    # from that same old base for many more fleets than the stale
    # exclusion alone explains — expected, not a regression.
    # `TestOutcomeDigestAgainstBase375` below re-proves the SAME shape of
    # invariant against THIS round's own base/head instead.
    @pytest.mark.parametrize("strategy", ["best", "consume-first"])
    def test_digest_matches_base_except_the_stale_exclusion(self, tmp_path, strategy):
        fleets, stale_by_fleet, head_results = self._head(tmp_path, strategy)
        base_results = _base_engine_results(
            tmp_path, strategy, self._DIGEST_SEED, self._N_FLEETS
        )
        # The consume-first HOLD keeps the `stale-usage` literal; a SKIP
        # (`proactive`, or `failover` on the fleet's own unhealthy-active
        # path — reachable under ANY strategy, `consume-first`/`dynamic`
        # included) abandons the candidate and goes on to switch or block,
        # citing a distinct literal so a log reader never sees a stall's
        # word ahead of a `Switched` line. Keyed on the OUTCOME, not the
        # strategy: a consume-first-strategy fleet's active account can
        # still fail over.

        diffs = [
            i for i in range(self._N_FLEETS) if head_results[i] != base_results[i]
        ]
        assert diffs, (
            "the fixture never exercised the exclusion on this seed — "
            "strengthen it before trusting the digest"
        )
        for i in diffs:
            # "The fleet carried a stale candidate somewhere" alone does not
            # exclude a ranking change on that same fleet for an unrelated
            # reason — pin it to the mechanism: HEAD itself must have hit
            # the exclusion, and BASE's landing account must be one this
            # fixture actually marked stale.
            assert stale_by_fleet[i], (
                f"fleet {i} differs with NO stale candidate — a scope error, "
                f"not the intended exclusion: head={head_results[i]!r} "
                f"base={base_results[i]!r}"
            )
            expected = "stale-usage" if head_results[i][0] == "NO_ACTION" else "stale-candidate-skipped"
            assert ("no-switch", expected) in head_results[i][2], (
                f"fleet {i} differs and carries a stale candidate, but "
                f"head's own events never cite {expected} — the "
                f"divergence traces to something else, not the exclusion: "
                f"head={head_results[i]!r}"
            )
            assert str(base_results[i][1]) in stale_by_fleet[i], (
                f"fleet {i}: base landed on account {base_results[i][1]!r}, "
                f"which this fixture never marked stale "
                f"({stale_by_fleet[i]!r}) — a ranking change, not base "
                f"landing on the candidate head correctly excluded: "
                f"base={base_results[i]!r}"
            )

        mutant_results = self._mutant(tmp_path, fleets, strategy)
        if strategy == "best":
            # `best`'s trigger is never the literal "consume-first"/"dynamic"
            # string, so pre-fix it never entered this gate at all (the base
            # engine's `if trigger in CONSUME_FIRST_STRATEGIES:` was False on
            # every `best` tick). There is no pre-existing freshness check at
            # this trigger for the blanket mutant to also wipe out, so it
            # reproduces base byte-for-byte.
            assert mutant_results == base_results, (
                f"neutralizing the exclusion should reproduce the pre-fix "
                f"engine exactly for a `{strategy}`-strategy fleet: "
                f"diffs={diffs}"
            )
        else:
            # `consume-first`/`dynamic` already ran a freshness check at this
            # gate BEFORE this PR (`trigger in CONSUME_FIRST_STRATEGIES` was
            # already true whenever `trigger = settings.strategy`), so this
            # PR only ADDS the `token_dead()` half — but that half is nested
            # inside the SAME `candidate_usage_is_stale` this mutant blanks
            # to `False`, so the mutant also neutralizes the pre-existing
            # freshness check and cannot reproduce base byte-for-byte here.
            # Bound it symmetrically to the head/base loop above: on every
            # fleet where the mutant diverges from BASE, base HELD (the hold
            # its own freshness check produced) and the mutant LANDED on a
            # candidate this fixture marked stale — never a ranking-order
            # change with no attributable cause.
            mutant_diffs = [
                i
                for i in range(self._N_FLEETS)
                if mutant_results[i] != base_results[i]
            ]
            assert mutant_diffs, (
                "the fixture never exercised the pre-existing freshness "
                "check this mutant also neutralizes on this seed — "
                "strengthen it before trusting the bound"
            )
            for i in mutant_diffs:
                assert stale_by_fleet[i], (
                    f"fleet {i} mutant/base divergence with NO stale "
                    f"candidate — a scope error, not the pre-existing "
                    f"freshness check the mutant over-neutralizes: "
                    f"mutant={mutant_results[i]!r} base={base_results[i]!r}"
                )
                assert base_results[i][:2] == ("NO_ACTION", 1), (
                    f"fleet {i}: base did not hold on its own freshness "
                    f"check: base={base_results[i]!r}"
                )
                assert str(mutant_results[i][1]) in stale_by_fleet[i], (
                    f"fleet {i}: the mutant landed on account "
                    f"{mutant_results[i][1]!r}, which this fixture never "
                    f"marked stale ({stale_by_fleet[i]!r}) — a ranking "
                    f"change, not the neutralized gate admitting a stale "
                    f"candidate: mutant={mutant_results[i]!r}"
                )


_ROUND_375_BASE_SHA = "a32a34d779ac5cb2838afe49df1cc41ec801be27"


class TestOutcomeDigest375:
    """F5 (#375): the motivating window, reconstructed as a named fixture
    from the coordinator/analyzer evidence (10:01:32Z Account-3 at 5h 78% /
    7d 56%, healthy; Account-5 at 7d 96%) — BASE (this round's own start,
    `_ROUND_375_BASE_SHA`) must switch Account-3 -> Account-5 exactly as
    the fleet did live, and HEAD must not (the whole point of #375). A
    mutant that undoes #375's two admission guards (the trigger's
    `about_to_wall` bar and the cold floor) must reproduce BASE's digest
    exactly — the control that the divergence traces to THOSE and nothing
    else moved. Composes (never subclasses, which would re-collect
    every one of its own tests under this class too) `TestOutcomeDigest
    AgainstBase`'s `_fleet`/`_run`/`_mutant`/`_head` machinery unchanged
    for the second half: `best`/`consume-first` stay byte-identical
    against THIS round's own base, over the same random fleets, with the
    same stale-exclusion mutant control still moving them.
    """

    _base = TestOutcomeDigestAgainstBase()

    @staticmethod
    def _motivating_fleet(now: float) -> dict:
        # Account numbers 1/2 (not the incident's own 3/5): `_run`
        # (`TestOutcomeDigestAgainstBase`, reused verbatim) always seeds
        # accounts 1/2/3 and makes 1 live — account 3 stays unseeded here.
        return {
            "1": {  # active ("Account-3" in the incident): healthy, well
                     # clear of about_to_wall
                "five_hour": {"pct": 78.0, "resets_at": _iso_at(now + 4 * 3600)},
                "seven_day": {"pct": 56.0, "resets_at": _iso_at(now + 5 * 86400)},
            },
            "2": {  # candidate ("Account-5"): near-empty, but resets first
                "five_hour": {"pct": 0.0, "resets_at": _iso_at(now + 4 * 3600)},
                "seven_day": {"pct": 96.0, "resets_at": _iso_at(now + 1 * 86400)},
            },
        }

    def _run_motivating(self, engine_cls, tmp_path, tag):
        raw = self._motivating_fleet(1_000_000.0)
        entries = {num: _entry_for(v, 1_000_000.0) for num, v in raw.items()}
        return self._base._run(engine_cls, tmp_path, tag, [entries], "dynamic")[0]

    def _run_motivating_mutant(self, tmp_path, tag, **settings_kwargs):
        """Same fixture as `_run_motivating`, but threads extra
        `AutoSwitchSettings` kwargs through -- the shared `_run` (never
        touched, per this class's own docstring) takes none.
        """
        raw = self._motivating_fleet(1_000_000.0)
        entries = {num: _entry_for(v, 1_000_000.0) for num, v in raw.items()}
        home = tmp_path / tag
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
            h = EngineHarness(home, strategy="dynamic", **settings_kwargs)
            h.seed(1, "a@example.com")
            h.seed(2, "b@example.com")
            h.seed(3, "c@example.com")
            h.make_live("a@example.com", 1)
            outcome = h.tick_with_entries(entries)
        events = tuple((e.kind, getattr(e, "reason", None)) for e in h.events)
        return outcome.name, h.active_number(), events

    def test_base_switches_head_does_not_and_the_mutant_restores_base(
        self, tmp_path
    ):
        head = self._run_motivating(AutoSwitchEngine, tmp_path, "head")
        assert head[0] == "NO_ACTION", (
            f"got {head!r} — #375's whole point: a healthy active "
            "(headroom 22) must not move for a soonest-resetting, "
            "near-empty candidate any more"
        )
        base_results = _base_engine_results(
            tmp_path, "dynamic", seed=0, n_fleets=1,
            base_sha=_ROUND_375_BASE_SHA,
            custom_fleets=[self._motivating_fleet(1_000_000.0)],
        )
        base = base_results[0]
        assert base[0] == "SWITCHED" and base[1] == 2, (
            f"got {base!r} — the reconstructed fixture must reproduce the "
            "live incident (Switched Account-3 -> Account-5, here "
            "accounts 1 -> 2) against this round's own start commit"
        )

        import claude_swap.autoswitch as autoswitch

        old_classify = autoswitch._classify_dynamic_trigger

        def always_proactive(active_headroom):
            # #375 replaced the bare below-threshold trigger with TWO new
            # guards, not one: `about_to_wall` deciding whether `proactive`
            # fires at all, and `cold_switch_cost_pct` (below) deciding
            # whether a real-headroom candidate is admissible once it
            # does. Undoing only the trigger still leaves the floor
            # blocking this fixture (measured) -- the drain mechanism that
            # used to make a lone-function patch sufficient is deleted
            # (item 3), so both revert together as the one thing #375's
            # design actually replaced.
            return "at-limit" if active_headroom <= 0 else "proactive"

        autoswitch._classify_dynamic_trigger = always_proactive
        try:
            mutant = self._run_motivating_mutant(
                tmp_path, "mutant", cold_switch_cost_pct=0.0
            )
        finally:
            autoswitch._classify_dynamic_trigger = old_classify
        assert mutant == base, (
            f"got {mutant!r}, want {base!r} — restoring the pre-#375 "
            "trigger and admission floor together must reproduce BASE's "
            "digest exactly"
        )
        assert head != base, (
            "the mutant control is meaningless if head already matched "
            "base without it"
        )

    @pytest.mark.parametrize("strategy", ["best", "consume-first"])
    def test_best_and_consume_first_digest_identical_against_this_rounds_base(
        self, tmp_path, strategy
    ):
        fleets, stale_by_fleet, head_results = self._base._head(tmp_path, strategy)
        base_results = _base_engine_results(
            tmp_path, strategy, self._base._DIGEST_SEED, self._base._N_FLEETS,
            base_sha=_ROUND_375_BASE_SHA,
        )
        assert head_results == base_results, (
            f"{strategy} must be byte-identical against this round's own "
            f"base — #375 touches `dynamic` only"
        )
        mutant_results = self._base._mutant(tmp_path, fleets, strategy)
        assert mutant_results != head_results, (
            "the existing stale-candidate mutant control must still move "
            "the digest — a no-op injection proves nothing"
        )

    @pytest.mark.parametrize("strategy", ["best", "consume-first"])
    def test_best_and_consume_first_digest_identical_above_the_walled_threshold(
        self, tmp_path, strategy
    ):
        """The digest above always runs at the DEFAULT departure threshold,
        so it can never drive an active into ``about_to_wall`` (<=3pt
        headroom) while ALSO staying below threshold — exactly the band
        `_walled_may_take_any_room`'s below-threshold `consume-first` gate
        needs (a threshold above ~97) to be reachable at all. It therefore
        could not have caught c6db55c4's unauthorized `consume-first`
        change; this raises the threshold into that band before trusting
        byte-identity again. At seed ``_DIGEST_SEED``, fleets 1/14/35 land
        their active in [97, 99) -- the positive control below fails loudly
        if that ever stops being true.
        """
        raised = {"threshold": 99.0}
        probe_rng = random.Random(self._base._DIGEST_SEED)
        active_pcts = [
            self._base._fleet(probe_rng, 1_000_000.0)[0]["1"].last_good[
                "five_hour"
            ]["pct"]
            for _ in range(self._base._N_FLEETS)
        ]
        assert any(97.0 <= p < 99.0 for p in active_pcts), (
            "the fixed-seed fixture no longer drives any fleet's active "
            "into the about_to_wall-but-below-threshold band — this test "
            "would pass vacuously without a real fleet to exercise"
        )
        fleets, _stale, head_results = self._base._head(
            tmp_path, strategy, **raised
        )
        base_results = _base_engine_results(
            tmp_path, strategy, self._base._DIGEST_SEED, self._base._N_FLEETS,
            base_sha=_ROUND_375_BASE_SHA, settings_kwargs=raised,
        )
        assert head_results == base_results, (
            f"{strategy} must be byte-identical against this round's own "
            f"base even with the departure threshold raised above 97, "
            f"where _walled_may_take_any_room's consume-first branch "
            f"would otherwise be reachable"
        )


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

    def test_api_key_used_on_proactive_when_no_oauth_peer_lands(self, temp_home):
        """PR #321 gate fix: `_usage(100)` above drives `at-limit`
        (headroom<=0), which never reaches `_tick_inner`'s admission gate at
        all -- so it cannot see the full `candidate_usage_is_stale` predicate
        this test targets. Real headroom (5) with the active over threshold
        drives `proactive` instead, and the API-key slot's sentinel entry is
        never fetched (`fetched_at` stays None forever) -- the full staleness
        bar refuses it permanently even though nothing about it is actually
        untrustworthy (no backoff, no failures, no strike)."""
        h = EngineHarness(temp_home, include_api_key_accounts=True)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        self._mark_api_key(h, 2)
        outcome = h.tick_with_usage({
            # #3 fails the landing/hysteresis gate: same utilization as the
            # active, no improvement to offer.
            "1": _usage(95), "2": "api key", "3": _usage(95),
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

    def test_at_limit_says_a_running_session_is_not_rescued(self, temp_home):
        """A switch changes the DEFAULT login. It cannot move a session-mode
        instance, which runs with CLAUDE_CONFIG_DIR on its own profile and its
        own `.credentials.json` — so escaping a limit for the account a live
        `cswap run` is using leaves that session exactly as stuck as it was.

        The engine already asks about live sessions, but only about the slot it
        is switching TO (`_freshen_target`). It never asks about the one it is
        LEAVING, which is the session that is actually blocked. The switch then
        succeeds, the event says so, and the user is still at their limit with
        nothing in the output saying why.

        This does not make the switch wrong — the next session gets the healthy
        account. It makes the SILENCE wrong.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=int(h.clock() * 1000) + 3_600_000)
        h.make_live("a@example.com", 1)
        # live pids for the ACTIVE slot only; the target must stay activatable
        # or _freshen_target skips it and there is no switch to inspect.
        with patch.object(
            h.switcher,
            "live_session_pids_for",
            side_effect=lambda num, email: [4242] if num == "1" else [],
        ):
            outcome = h.tick_with_usage({"1": _usage(100), "2": _usage(10)})
        assert outcome is TickOutcome.SWITCHED
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "at-limit"
        joined = " ".join(sw.warnings).lower()
        assert "4242" in joined and "session" in joined, (
            "the escape must say that the live session-mode instance on the "
            "account it just left is NOT moved by this switch — it names the "
            f"pid so the user can act on it. warnings were: {sw.warnings!r}"
        )

    def test_control_no_live_session_carries_no_such_warning(self, temp_home):
        """CONTROL. Without a live session on the active slot the warning must
        be absent — otherwise the message is decoration rather than a fact."""
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=int(h.clock() * 1000) + 3_600_000)
        h.make_live("a@example.com", 1)
        with patch.object(
            h.switcher, "live_session_pids_for", side_effect=lambda num, email: []
        ):
            outcome = h.tick_with_usage({"1": _usage(100), "2": _usage(10)})
        assert outcome is TickOutcome.SWITCHED
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        joined = " ".join(sw.warnings).lower()
        assert "session-mode" not in joined, (
            f"no live session, so no live-session warning. got: {sw.warnings!r}"
        )

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
        # "Across instances" means a restart: the old engine is gone before
        # the new one exists. Stopping it releases the LIVE lock, so the
        # successor comes up LIVE — a fresh engine that silently demoted
        # itself would prove nothing about quarantine persistence.
        harness.engine.stop()
        fresh_engine = harness._make_engine()
        assert not fresh_engine.dry_run
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

    def test_a_quarantine_recorded_blind_is_not_lifted_by_a_readable_tick(
        self, harness
    ):
        """Guarding the RELEASE is half of it: the RECORD can be blind too.

        A read that failed when the quarantine was written fingerprints as
        None -- the same value a genuinely ABSENT backup records -- and the
        release then reads any later readable credential as "the user
        replaced it". The credential here never changes.
        """
        store = harness.switcher._store
        real_read = store._read_account_credentials

        def unreadable(account_num, email, failed=None):
            if account_num == "2":
                if failed is not None:
                    failed.append(True)
                return ""
            return real_read(account_num, email, failed)

        # Blind at RECORD time only; the read works for every later tick.
        with patch.object(
            store, "_read_account_credentials", side_effect=unreadable,
        ):
            assert harness.switcher._read_account_credentials_ex(
                "2", "b@example.com") == ("", True), (
                "premise: the record-time read must report the failed verdict"
            )
            harness.engine._quarantine("2", "b@example.com", "identity-conflict")

        entry = (harness.state().get("quarantine") or {}).get("2")
        assert entry is not None, "premise: the slot must be quarantined"
        # PREMISE that holds in BOTH worlds: the record is blind. Asserting
        # the flag here instead would fail before the harm on the unfixed
        # code, where the key does not exist at all.
        assert entry.get("refreshTokenFingerprint") is None, (
            "premise: the record-time read failed, so no generation was learned"
        )
        harness.events.clear()

        harness.tick_with_usage({
            "1": _usage(95), "2": _usage(0), "3": _usage(50),
        })

        assert "2" in (harness.state().get("quarantine") or {}), (
            "DEFECT: a quarantine whose generation was never learned was "
            "released on a compare against a value nobody measured. For an "
            "identity conflict nothing re-checks before the switch, so the "
            "engine lands on the barred slot with every gauge normal"
        )
        assert not any(isinstance(e, UnquarantineEvent) for e in harness.events)
        # ...and the readable tick BOUND it, so the slot is not stuck blind.
        bound = (harness.state().get("quarantine") or {}).get("2")
        assert bound.get("fingerprintUnknown") is False, (
            "the tick that could read it must bind the generation, or the "
            "slot can never be released by an ordinary re-login"
        )
        assert bound.get("refreshTokenFingerprint"), (
            "binding must record the generation it just read"
        )

    def test_the_documented_recovery_lifts_a_blind_quarantine(self, harness):
        """`--add-account --slot N` is the recovery the product prints.

        A blind record has no generation to compare against, so the bind
        that keeps a transient read failure from releasing it would also
        take the REPLACEMENT as the quarantine's own generation. Every
        later compare then matches and the slot stays barred with nothing
        said — the user follows the printed instruction and watches it do
        nothing. The roster's own stamp separates the two.
        """
        store = harness.switcher._store
        real_read = store._read_account_credentials

        def unreadable(account_num, email, failed=None):
            if account_num == "2":
                if failed is not None:
                    failed.append(True)
                return ""
            return real_read(account_num, email, failed)

        with patch.object(
            store, "_read_account_credentials", side_effect=unreadable,
        ):
            harness.engine._quarantine("2", "b@example.com", "identity-conflict")

        entry = (harness.state().get("quarantine") or {}).get("2")
        # PREMISE, true in both worlds: the record carries no generation.
        assert entry is not None
        assert entry.get("refreshTokenFingerprint") is None

        # The recovery: a fresh login captured into the same slot.
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-recovered", "refreshToken": "rt-recovered",
            }}),
        )
        data = harness.switcher._get_sequence_data()
        data["accounts"]["2"]["added"] = "2099-01-01T00:00:00Z"
        harness.switcher._write_json(harness.switcher.sequence_file, data)
        # PREMISE: the re-add is recorded strictly after the quarantine.
        assert data["accounts"]["2"]["added"] > entry["at"]
        harness.events.clear()

        harness.tick_with_usage({
            "1": _usage(95), "2": _usage(0), "3": _usage(50),
        })

        assert "2" not in (harness.state().get("quarantine") or {}), (
            "DEFECT: the user ran the recovery the quarantine notice names "
            "and the tick bound the replacement as the quarantine's own "
            "generation instead of releasing on it, so the slot stays barred"
        )
        assert any(
            isinstance(e, UnquarantineEvent) for e in harness.events
        ), "the release must be announced"

    def test_an_import_recovery_lifts_a_blind_quarantine(
        self, harness, tmp_path
    ):
        """`--import --force` replaces a slot's credential too.

        The roster stamp is what separates a recovery from an unchanged
        slot, so every command that replaces the credential has to move it.
        The import used to copy the bundle's stamp, which is a fact about
        the EXPORT -- older than any later quarantine by construction -- so
        the bind took the restored credential as the quarantine's own
        generation and the slot stayed barred. A second import writes the
        same bytes, so it does not free it either.
        """
        from claude_swap.transfer import import_accounts

        store = harness.switcher._store
        real_read = store._read_account_credentials

        def unreadable(account_num, email, failed=None):
            if account_num == "2":
                if failed is not None:
                    failed.append(True)
                return ""
            return real_read(account_num, email, failed)

        # The quarantine is recorded first; the user reads the notice and acts
        # later. Freeze its stamp rather than race the import inside one second.
        with patch.object(store, "_read_account_credentials", side_effect=unreadable), \
             patch("claude_swap.autoswitch._now_iso", return_value="2024-06-01T00:00:00Z"):
            harness.engine._quarantine("2", "b@example.com", "identity-conflict")
        entry = (harness.state().get("quarantine") or {})["2"]
        at = entry["at"]
        assert entry["refreshTokenFingerprint"] is None, "premise: blind record"

        # A bundle exported on a healthy day: it carries the ORIGINAL added.
        bundle = {"version": 1, "accounts": [{
            "email": "b@example.com", "number": 2, "uuid": "uuid-2",
            "organizationUuid": "", "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
            "credentials": {"claudeAiOauth": {"accessToken": "sk-RESTORED",
                                              "refreshToken": "rt-RESTORED"}},
            "config": {"oauthAccount": {"emailAddress": "b@example.com",
                                        "accountUuid": "uuid-2"}},
        }]}
        f = tmp_path / "backup.json"
        f.write_text(json.dumps(bundle))
        import_accounts(harness.switcher, str(f), force=True)

        added = harness.switcher._get_sequence_data()["accounts"]["2"]["added"]
        restored = harness.switcher._read_account_credentials("2", "b@example.com")
        print(f"\nafter import: added={added!r}  at={at!r}  added>at={added > at}")
        print(f"credential restored: {'sk-RESTORED' in (restored or '')}")
        # PREMISES: the import really replaced the credential.
        assert "sk-RESTORED" in (restored or ""), "premise: the import must land"
        harness.events.clear()
        for i in range(3):
            harness.tick_with_usage({"1": _usage(95), "2": _usage(0), "3": _usage(50)})
        q = harness.state().get("quarantine") or {}
        print(f"after 3 ticks: quarantined={'2' in q}  unq events={sum(isinstance(e, UnquarantineEvent) for e in harness.events)}")
        assert "2" not in q, (
            "DEFECT: the user restored a working credential with the product's own "
            f"import and the slot is still barred: {q.get('2')}"
        )

    def test_a_swap_carries_the_bar_to_the_account_s_new_slot(self, harness):
        """The bar is on a slot; `swap` and `move` move the account.

        Both exchange the roster rows AND the credentials, so the barred
        lineage lands on another number while this one has correctly
        stopped being about it. Released there, it is back in rotation
        immediately -- and `_freshen_target` answers "ok" without
        consuming a grant, so nothing re-checks the identity before the
        engine switches into it.
        """
        from claude_swap.autoswitch import SwitchEvent

        harness.engine._quarantine("2", "b@example.com", "identity-conflict")
        assert "2" in (harness.state().get("quarantine") or {}), "premise: slot 2 barred"

        harness.switcher.swap_accounts("2", "3")
        roster = harness.switcher._get_sequence_data()["accounts"]
        print(f"\nafter swap: slot2={roster['2']['email']}  slot3={roster['3']['email']}")
        # PREMISE: the barred ACCOUNT is now at slot 3.
        assert roster["3"]["email"] == "b@example.com"

        harness.events.clear()
        harness.tick_with_usage({"1": _usage(95), "2": _usage(100), "3": _usage(0)})
        q = harness.state().get("quarantine") or {}
        switched = [e for e in harness.events if isinstance(e, SwitchEvent)]
        unq = [e for e in harness.events if isinstance(e, UnquarantineEvent)]
        print(f"quarantine={list(q)}  unq={len(unq)}  switched={[getattr(e,'number',None) for e in switched]}")
        print(f"active={harness.active_number()}")
        assert harness.active_number() != 3, (
            "DEFECT: the swap moved the barred lineage to slot 3 and the bar "
            "stayed on slot 2, where it was correctly dropped as account-replaced; "
            "one tick later the engine is logged in on the account it barred"
        )

    def test_a_bar_is_not_carried_onto_a_same_address_sibling(
        self, harness, temp_home
    ):
        """An address is not an account.

        The personal/org pattern puts one address in two slots, and the
        codebase keys accounts on the `(email, organizationUuid)` composite
        everywhere else. Carrying on the address alone moves the bar onto
        the sibling -- and a blind record then BINDS that sibling's own
        generation, so no later compare can ever lift it.
        """
        _seed_org_twin(harness, 4, "b@example.com", "org-acme")

        # BLIND at record time: with a generation recorded, the next tick's
        # ordinary compare releases the carried bar and the harm is invisible.
        store = harness.switcher._store
        real_read = store._read_account_credentials

        def unreadable(account_num, email, failed=None):
            if account_num == "2":
                if failed is not None:
                    failed.append(True)
                return ""
            return real_read(account_num, email, failed)

        with patch.object(store, "_read_account_credentials", side_effect=unreadable):
            harness.engine._quarantine("2", "b@example.com", "identity-conflict")
        assert (harness.state()["quarantine"]["2"]).get("fingerprintUnknown") is True, (
            "premise: the record must be blind"
        )
        assert "2" in (harness.state().get("quarantine") or {}), "premise: slot 2 barred"
        # PREMISE: two slots share the email and differ only by org.
        r = harness.switcher._get_sequence_data()["accounts"]
        assert r["2"]["email"] == r["4"]["email"] and r["2"]["organizationUuid"] != r["4"]["organizationUuid"]

        harness.switcher.remove_account("2", assume_yes=True)
        harness.events.clear()
        for _ in range(4):
            harness.tick_with_usage({"1": _usage(95), "3": _usage(50), "4": _usage(0)})

        q = harness.state().get("quarantine") or {}
        unq = [e for e in harness.events if isinstance(e, UnquarantineEvent)]
        assert "4" not in q, (
            "DEFECT: the bar was carried onto slot 4, a DIFFERENT account that "
            "shares only the email; the blind-bind then wrote slot 4's own "
            "fingerprint so no later compare can lift it"
        )

    def test_two_barred_slots_that_exchanged_keep_both_bars(
        self, harness, temp_home
    ):
        """A slot that is vacating is a place a bar may move to.

        Excluding every quarantined slot makes each carry blind to the
        other exactly when both moved, so one swap drops both bars and the
        engine is free to switch into the account it barred.
        """
        _seed_org_twin(harness, 4, "b@example.com", "org-acme")

        harness.engine._quarantine("2", "b@example.com", "identity-conflict")
        harness.engine._quarantine("3", "c@example.com", "identity-conflict")
        q0 = harness.state().get("quarantine") or {}
        assert set(q0) == {"2", "3"}, f"premise: both barred, got {list(q0)}"

        harness.switcher.swap_accounts("2", "3")
        r = harness.switcher._get_sequence_data()["accounts"]
        harness.events.clear()
        harness.tick_with_usage({"1": _usage(95), "2": _usage(0), "3": _usage(0), "4": _usage(100)})

        q = harness.state().get("quarantine") or {}
        unq = [(e.number, e.reason) for e in harness.events if isinstance(e, UnquarantineEvent)]
        assert set(q) == {"2", "3"}, (
            "DEFECT: swapping two barred slots dropped BOTH bars -- each carry "
            f"could not see the other because it is itself quarantined: {list(q)}"
        )

    def test_a_legacy_record_never_carries_a_bar(self, harness):
        """A record written before the composite names an ADDRESS.

        On the personal/org pair it cannot say which slot now holds the
        barred account, and carrying on the address alone puts the bar on
        the sibling -- where the blind bind writes THAT account's own
        generation and no later compare can lift it. Releasing is what
        this did before the carry existed; a guess is not.
        """
        _seed_org_twin(harness, 4, "b@example.com", "org-acme")

        """Every record the code wrote BEFORE the composite landed has no org
        key, and nothing upgrades one."""
        _blind_quarantine(harness, "2", "b@example.com")
        # Make it LEGACY, exactly as a record written by the shipped code is.
        def strip(state):
            state["quarantine"]["2"].pop("organizationUuid", None)
        harness.engine._mutate_state(strip)
        entry = harness.state()["quarantine"]["2"]
        assert "organizationUuid" not in entry, "premise: the record is legacy"
        assert entry.get("fingerprintUnknown") is True, "premise: blind"

        harness.switcher.remove_account("2", assume_yes=True)
        harness.events.clear()
        for _ in range(4):
            harness.tick_with_usage({"1": _usage(95), "3": _usage(50), "4": _usage(0)})

        q = harness.state().get("quarantine") or {}
        unq = [e for e in harness.events if isinstance(e, UnquarantineEvent)]
        assert "4" not in q, (
            "DEFECT: a LEGACY record matched on the address alone and carried the "
            "bar onto the org sibling; the blind bind then wrote slot 4's own "
            "generation so no later compare can lift it"
        )

    def test_a_release_does_not_eat_a_bar_carried_onto_the_same_slot(
        self, harness
    ):
        """A slot whose own record has nowhere to go is released, and the
        same slot is a legal carry TARGET -- so popping after writing drops
        the bar that just arrived, with no event naming its account."""
        _seed_org_twin(harness, 4, "b@example.com", "org-acme")

        """A slot can be both a carry TARGET and its own release source."""
        _blind_quarantine(harness, "2", "b@example.com")
        _blind_quarantine(harness, "3", "c@example.com")
        assert set(harness.state()["quarantine"]) == {"2", "3"}, "premise"

        harness.switcher.swap_accounts("2", "3")
        harness.switcher.remove_account("2", assume_yes=True)   # c@ leaves
        r = harness.switcher._get_sequence_data()["accounts"]
        assert "3" in r and r["3"]["email"] == "b@example.com", "premise: b@ is at slot 3"
        harness.events.clear()
        harness.tick_with_usage({"1": _usage(95), "3": _usage(0), "4": _usage(100)})

        q = harness.state().get("quarantine") or {}
        unq = [(e.number, e.email, e.reason) for e in harness.events
               if isinstance(e, UnquarantineEvent)]
        sw = [e for e in harness.events if isinstance(e, SwitchEvent)]
        assert "3" in q, (
            "DEFECT: the bar on b@ was carried to slot 3 and then popped by slot "
            f"3's OWN release, with no event naming b@; unq={unq}"
        )
        assert harness.active_number() != 3, (
            "DEFECT: the engine switched into the account it barred"
        )

    def test_an_org_backfill_is_not_the_account_moving(self, harness, temp_home):
        """`_migrate_org_fields` backfills `organizationUuid` on a pre-org
        row, and every other caller reads the migrated roster. Reading the
        plain one at either end makes that backfill look like a move."""

        """`_quarantine` and the release read the UNMIGRATED roster; every other
        caller reads the migrated one, which backfills organizationUuid."""
        d = harness.switcher._get_sequence_data()
        d["accounts"]["3"].pop("organizationUuid", None)      # a pre-org roster row
        harness.switcher._write_json(harness.switcher.sequence_file, d)
        harness.switcher._write_account_config("3", "c@example.com", json.dumps(
            {"oauthAccount": {"emailAddress": "c@example.com", "accountUuid": "uuid-3",
                              "organizationUuid": "org-backfilled",
                              "organizationName": "Backfilled"}}))
        # PREMISE, true in BOTH worlds: the roster row carries no org yet.
        assert "organizationUuid" not in harness.switcher._get_sequence_data()[
            "accounts"]["3"]
        harness.engine._quarantine("3", "c@example.com", "invalid_grant")
        rec = harness.state()["quarantine"]["3"]

        harness.switcher._get_sequence_data_migrated()        # the backfill any command runs
        row = harness.switcher._get_sequence_data()["accounts"]["3"]
        assert row.get("organizationUuid") == "org-backfilled", "premise: the backfill ran"
        harness.events.clear()
        harness.tick_with_usage({"1": _usage(95), "2": _usage(50), "3": _usage(0), "4": _usage(50)})

        q = harness.state().get("quarantine") or {}
        unq = [(e.number, e.reason) for e in harness.events if isinstance(e, UnquarantineEvent)]
        assert "3" in q, (
            f"DEFECT: an org BACKFILL read as the account moving and released a "
            f"standing bar with the false reason account-replaced; unq={unq}"
        )

    def test_an_unreadable_backup_does_not_lift_a_quarantine(self, harness):
        """"Could not read it" is not "the user replaced it".

        The plain reader answers "" for a failed read and for an absent one
        alike, so one locked Keychain or one EACCES fingerprints as None,
        differs from the recorded value, and drops the quarantine for good.
        An identity-conflict quarantine dropped that way does not re-arm.
        """
        harness.engine._quarantine("2", "b@example.com", "identity-conflict")
        assert "2" in (harness.state().get("quarantine") or {}), (
            "premise: the slot must start quarantined"
        )
        harness.events.clear()

        # Patched at the STORE, the one reader BOTH paths go through, so the
        # case exercises the same failure whichever reader the code uses --
        # a patch on the `_ex` variant alone would leave the plain reader
        # answering the real credential and the case would pass for the
        # wrong reason.
        store = harness.switcher._store
        real_read = store._read_account_credentials

        def unreadable(account_num, email, failed=None):
            if account_num == "2":
                if failed is not None:
                    failed.append(True)
                return ""
            return real_read(account_num, email, failed)

        with patch.object(
            store, "_read_account_credentials", side_effect=unreadable,
        ):
            # PREMISE: both readers now report the canonical failed-read
            # verdicts, or this case is not about an unreadable backup.
            assert harness.switcher.read_account_credentials(
                "2", "b@example.com") == ""
            assert harness.switcher._read_account_credentials_ex(
                "2", "b@example.com") == ("", True)
            harness.tick_with_usage({
                "1": _usage(95), "2": _usage(0), "3": _usage(50),
            })

        assert "2" in (harness.state().get("quarantine") or {}), (
            "DEFECT: a transient read failure released the quarantine. The "
            "reason recorded is `credentials-replaced`, which is false, and "
            "for an identity conflict nothing re-checks before the switch: "
            "the engine then switches onto the barred slot with every gauge "
            "reading normal"
        )
        assert not any(isinstance(e, UnquarantineEvent) for e in harness.events)

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


class TestSessionStrategy:
    """apply_strategy(): the TUI's session-only, mid-run override. The
    existing coverage (test_tui.py's test_strategy_cycle_is_session_only)
    only asserts against `_FakeEngine.applied_strategies`, never a real
    `AutoSwitchEngine` — the whole suite passes with `apply_strategy`'s body
    replaced by `pass`. This is the real-engine check."""

    def test_apply_strategy_retargets_settings(self, harness):
        assert harness.engine.settings.strategy == "consume-first"
        harness.engine.apply_strategy("dynamic")
        assert harness.engine.settings.strategy == "dynamic"


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

    def test_poll_event_under_dynamic_shows_the_derived_switch_bar(self, temp_home):
        h = EngineHarness(temp_home, threshold=90.0, strategy="dynamic")
        h.seed(1, "a@example.com")
        h.make_live("a@example.com", 1)
        h.tick_with_usage({"1": _usage(60)})
        poll = next(e for e in h.events if isinstance(e, PollEvent))
        assert "switch at 97%" in poll.human()

    def test_below_threshold_detail_shows_fractional_threshold(self, temp_home):
        h = EngineHarness(temp_home, threshold=99.9, strategy="best")
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
        h = EngineHarness(temp_home, threshold=99.9, strategy="best")
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
        h = self._seed(temp_home, strategy="best")
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
        # blocked-cadence fallback instead of a reset sleep). #2's 5h is
        # ALSO maxed (not just Fable): a model-only block is no longer a
        # blackout (`_rank_candidates`'s retry ranks around it on 5h/7d),
        # so this fleet needs a genuine dual exhaustion to stay one.
        h = self._seed(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _model_usage(95, 10),
            "2": {
                "five_hour": {"pct": 100.0},  # no resets_at
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

    def test_scoped_only_block_is_not_a_blackout_and_switches(self, temp_home):
        # #2 and #3 are blocked ONLY by Fable — 5h/7d both have room (3%
        # used). That is no longer treated as exhaustion: `_rank_candidates`
        # drops the model window once every candidate is blocked only by it
        # and ranks on 5h/7d instead, so the engine moves rather than
        # waiting out the Fable reset. (Was `BLOCKED` with the wake time
        # taken from the scoped reset — the exact shape this fixes.)
        # The retry that rescues it is `dynamic`-only (see
        # TestAModelWindowIsNotABlackout's `_args`); `best`/`consume-first`
        # keep today's behaviour and stay blocked here, unchanged.
        h = self._seed(temp_home, model="Fable", strategy="dynamic")
        fable_reset = "2026-07-06T09:00:00Z"
        blocked = {
            "five_hour": {"pct": 3.0, "resets_at": "2026-07-05T12:00:00Z"},
            "seven_day": {"pct": 0.0},
            "scoped": [{"name": "Fable", "pct": 100.0, "resets_at": fable_reset}],
        }
        # Active headroom 3 (#375's `about_to_wall` bar — its own 5h/7d
        # windows are otherwise wide open too, same as #2/#3, so this is
        # about the model-window retry, not about being genuinely spent).
        outcome = h.tick_with_usage({
            "1": _model_usage(97, 10), "2": blocked, "3": blocked,
        })
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome} — #2/#3's only over-bar window is Fable, with "
            "5h/7d wide open, so the fleet is not exhausted"
        )
        assert h.active_number() in (2, 3)

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


class TestAModelWindowIsNotABlackout:
    """A pinned model's scoped window is folded into every headroom read
    alongside 5h/7d — so a candidate whose ONLY over-bar window is the model
    was dropped as an unhealthy landing exactly like one genuinely spent on
    5h/7d, and when every candidate carries that same model bar the ranking
    emptied while 5h/7d headroom went unused. ``_rank_candidates`` retries
    once on 5h/7d alone when the model-gated pass comes back empty; a
    candidate blocked on 5h/7d too stays blocked in that retry, so a real
    blackout is untouched.
    """

    def _args(self, harness, *, usage, current, oauth_candidates, headroom,
              active_headroom, trigger=None, consume_first=True,
              strategy="dynamic"):
        # The retry this class tests (`_rank_candidates`'s 5h/7d fallback
        # pass) is gated to `strategy == "dynamic"` — `best`/`consume-first`
        # never re-rank on 5h/7d alone, by the owner's word that they must
        # read exactly as deployed today. `trigger` defaults to the
        # strategy name (the voluntary below-threshold trigger an engine
        # actually produces for it) unless a test names a specific trigger.
        return dict(
            trigger=trigger if trigger is not None else strategy,
            consume_first=consume_first,
            no_return=None,
            oauth_candidates=oauth_candidates,
            usage=usage,
            headroom=headroom,
            current=current,
            active_headroom=active_headroom,
            settings=AutoSwitchSettings(threshold=90.0, strategy=strategy),
            now=harness.clock.now,
        )

    def test_the_owners_fleet_moves_once_the_model_window_is_dropped(
        self, temp_home
    ):
        """The reported fleet, reproduced exactly: six accounts, Fable
        pinned, threshold 90. 1/3/4/5's only over-bar window is Fable — 5h
        and 7d both have room — so the model-gated pass empties and the
        retry on 5h/7d alone must both rescue it and rank it (soonest 7-day
        reset first, the consume-first strategy's own key). #2 stays out
        either way: its OWN 5h sits at the bar with no model involved.

        The active (#6) also reports its own over-bar Fable window — every
        account genuinely pinned to a model reports that model's window,
        and this round's fix (`_model_window_binds_everywhere`) requires
        the ACTIVE to be walled too before the retry is allowed to drop the
        model set (never a candidate-only reading): an active with real
        Fable headroom must not have the wall dropped just because its
        candidates all happen to be model-blocked.
        """
        h = EngineHarness(temp_home, model="Fable", threshold=90.0)
        now = h.clock.now

        def usage(five_h, seven_d, fable, days_out):
            return {
                "five_hour": {"pct": five_h},
                "seven_day": {
                    "pct": seven_d,
                    "resets_at": _iso_at(now + days_out * 86400),
                },
                "scoped": [{"name": "Fable", "pct": fable}],
            }

        fleet_usage = {
            "6": {
                "five_hour": {"pct": 72.0},
                "seven_day": {
                    "pct": 0.0, "resets_at": _iso_at(now + 100 * 86400),
                },
                "scoped": [{"name": "Fable", "pct": 95.0}],
            },
            "1": usage(34, 69, 91, 4),
            "2": usage(90, 79, 87, 0.5),
            "3": usage(33, 69, 94, 3),
            "4": usage(0, 64, 91, 2),
            "5": usage(0, 62, 90, 1),
        }
        headroom = {
            num: oauth.account_headroom(val, ("Fable",))
            for num, val in fleet_usage.items()
        }
        args = self._args(
            h, usage=fleet_usage, current="6",
            oauth_candidates=["1", "2", "3", "4", "5"],
            headroom=headroom, active_headroom=headroom["6"],
        )
        ordered, any_known, _, _ = h.engine._rank_candidates(**args)
        assert any_known
        assert list(ordered) == ["5", "4", "3", "1"], (
            f"got {list(ordered)} — every candidate's only over-bar window "
            "is Fable, 5h/7d has room on all of 1/3/4/5, and #2 must stay "
            "excluded on its own 5h at 90%: the retry is not a blanket "
            "unblock, only a re-rank on 5h/7d"
        )

    def test_the_retry_drops_the_model_gate_from_both_admission_and_ranking(
        self, temp_home
    ):
        """Candidates on BOTH sides of the model bar, unlike the owner's
        fleet above (its four ranked candidates all land in the same
        ``consume_first_rank_key`` tier, so a retry that kept re-gating on
        Fable internally would still rank them identically and the test
        could not tell). #1's ONLY good axis is 5h/7d (Fable 99% blocks it
        on the model axis); #3 is genuinely servable on both axes but with
        less 5h/7d headroom than #1; #2 has real 5h/7d headroom but not
        enough to beat the ACTIVE's — every one of these needs the retry's
        `models=()`/`fallback_headroom[current]` pair intact to land right.
        """
        h = EngineHarness(temp_home, model="Fable", threshold=90.0)

        def usage(five_h, fable):
            return {
                "five_hour": {"pct": five_h}, "seven_day": {"pct": 0.0},
                "scoped": [{"name": "Fable", "pct": fable}],
            }

        fleet_usage = {
            "0": usage(20.0, 95.0),   # active: 5h/7d headroom 80, model-gated 5
            "1": usage(5.0, 99.0),    # 5h/7d headroom 95, model-gated 1
            "2": usage(70.0, 100.0),  # 5h/7d headroom 30 — worse than active's 80
            "3": usage(8.0, 91.0),    # 5h/7d headroom 92, model-gated 9
        }
        headroom = {
            num: oauth.account_headroom(val, ("Fable",))
            for num, val in fleet_usage.items()
        }
        args = self._args(
            h, usage=fleet_usage, current="0",
            oauth_candidates=["1", "2", "3"],
            headroom=headroom, active_headroom=headroom["0"],
            trigger="proactive",
        )
        ordered, any_known, _, _ = h.engine._rank_candidates(**args)
        assert any_known
        assert list(ordered) == ["1", "3"], (
            f"got {list(ordered)} — #2 must stay excluded (worse than the "
            "active's real 5h/7d headroom), and #1 must rank ahead of #3 "
            "(more 5h/7d headroom): a retry that re-applies the model gate "
            "internally instead flips this order (#1 reads unservable on "
            "Fable and #3 wins) or drops #1 as unhealthy outright"
        )

    def test_a_real_blackout_stays_empty_after_the_retry(self, temp_home):
        """#2 and #3 are ALSO over the bar on 5h, not just Fable — the retry
        on 5h/7d alone must not manufacture a landing that was never there."""
        h = EngineHarness(temp_home, model="Fable", threshold=90.0)
        active = {
            "five_hour": {"pct": 20.0}, "seven_day": {"pct": 0.0},
            "scoped": [{"name": "Fable", "pct": 10.0}],
        }
        blocked = {
            "five_hour": {"pct": 95.0}, "seven_day": {"pct": 0.0},
            "scoped": [{"name": "Fable", "pct": 95.0}],
        }
        fleet_usage = {"1": active, "2": blocked, "3": blocked}
        headroom = {
            num: oauth.account_headroom(val, ("Fable",))
            for num, val in fleet_usage.items()
        }
        args = self._args(
            h, usage=fleet_usage, current="1", oauth_candidates=["2", "3"],
            headroom=headroom, active_headroom=headroom["1"],
        )
        ordered, _, _, _ = h.engine._rank_candidates(**args)
        assert list(ordered) == [], (
            f"got {list(ordered)} — #2 and #3 are genuinely spent on 5h "
            "too, so dropping the model window must not rescue them"
        )

    def test_the_fallback_does_not_engage_when_the_model_set_already_lands(
        self, temp_home
    ):
        """#2 is healthier once Fable is dropped than #1's Fable-gated pick
        — if the retry ran anyway and got merged in, #2 would win. It must
        never run: the model-gated pass already found #1, and that stays
        the answer."""
        h = EngineHarness(temp_home, model="Fable", threshold=90.0)
        active = {
            "five_hour": {"pct": 50.0}, "seven_day": {"pct": 0.0},
            "scoped": [{"name": "Fable", "pct": 0.0}],
        }
        usage_1 = {  # eligible on Fable too
            "five_hour": {"pct": 5.0}, "seven_day": {"pct": 0.0},
            "scoped": [{"name": "Fable", "pct": 20.0}],
        }
        usage_2 = {  # model-only blocked; would win once Fable is dropped
            "five_hour": {"pct": 5.0}, "seven_day": {"pct": 0.0},
            "scoped": [{"name": "Fable", "pct": 95.0}],
        }
        fleet_usage = {"3": active, "1": usage_1, "2": usage_2}
        headroom = {
            num: oauth.account_headroom(val, ("Fable",))
            for num, val in fleet_usage.items()
        }
        args = self._args(
            h, usage=fleet_usage, current="3", oauth_candidates=["1", "2"],
            headroom=headroom, active_headroom=headroom["3"],
            trigger="proactive", consume_first=False,
        )
        ordered, _, _, _ = h.engine._rank_candidates(**args)
        assert list(ordered) == ["1"], (
            f"got {list(ordered)} — the model-gated pass already found #1 "
            "eligible; #2 must never enter the ranking"
        )

    def test_a_real_blackout_at_the_limit_keeps_the_first_passs_waiting_flag(
        self, temp_home
    ):
        """A genuine blackout (5h AND 7d spent too, not just Fable) must
        report the same `waiting` verdict the model-gated pass reached, not
        whatever the 5h/7d-only retry happens to compute. Rigged so the two
        passes disagree: the active's ONLY knowable reset is Fable's scoped
        window, so the model-gated pass can name a recovery moment
        (`waiting=True`) while the 5h/7d-only retry sees no knowable reset at
        all and reports `waiting=False`. Returning the retry's tuple here —
        the bug this cut fixes — silently drops the wait."""
        h = EngineHarness(temp_home, model="Fable", threshold=90.0)
        now = h.clock.now
        fleet_usage = {
            "1": {
                "five_hour": {"pct": 100.0},
                "seven_day": {"pct": 100.0},
                "scoped": [
                    {"name": "Fable", "pct": 100.0, "resets_at": _iso_at(now + 3600)}
                ],
            },
            "2": {
                "five_hour": {"pct": 100.0},
                "seven_day": {"pct": 100.0},
                "scoped": [{"name": "Fable", "pct": 100.0}],
            },
        }
        headroom = {
            num: oauth.account_headroom(val, ("Fable",))
            for num, val in fleet_usage.items()
        }
        args = self._args(
            h, usage=fleet_usage, current="1", oauth_candidates=["2"],
            headroom=headroom, active_headroom=headroom["1"],
            trigger="at-limit", consume_first=False,
        )
        ordered, any_known, _, waiting = h.engine._rank_candidates(**args)
        assert list(ordered) == [], (
            f"got {list(ordered)} — #2 is genuinely spent on 5h/7d too, so "
            "dropping the model window must not rescue it"
        )
        assert any_known
        assert waiting is True, (
            "waiting came back False — the retry's tuple leaked through "
            "instead of the model-gated pass's, which had a knowable "
            "recovery moment (Fable's reset) the retry cannot see"
        )

    def test_classify_open_full_and_model_only(self):
        """The three outcomes ``classify_candidate_block`` reports, read by
        both the panel and the decision log so they cannot disagree."""
        assert classify_candidate_block(
            [("5h", 10.0), ("7d", 5.0), ("Fable", 20.0)], 90.0
        ) == ("open", None)
        assert classify_candidate_block(
            [("5h", 95.0), ("7d", 5.0), ("Fable", 10.0)], 90.0
        ) == ("full", "5h")
        assert classify_candidate_block(
            [("5h", 5.0), ("7d", 0.0), ("Fable", 95.0)], 90.0
        ) == ("model", "Fable")

    def test_classify_candidate_block_blocks_on_the_threshold_itself(self):
        """`>=`, not `>`: a window sitting exactly ON the threshold is a
        landing gate refusal too (`_rank_candidates_pass`'s own arithmetic
        this classifier mirrors), not an ``"open"`` slot."""
        assert classify_candidate_block(
            [("5h", 5.0), ("7d", 0.0), ("Fable", 90.0)], 90.0
        ) == ("model", "Fable")

    def test_classify_full_names_the_binding_window(self):
        """A ``"full"`` block must name which 5h/7d window blocked it, the
        same way a ``"model"`` block already names its window — a caller
        cannot otherwise render `<window> full` instead of a bare
        "blocked". #5's real values: 5h 0%, 7d 90%, Fable 100%, threshold 90."""
        assert classify_candidate_block(
            [("5h", 0.0), ("7d", 90.0), ("Fable", 100.0)], 90.0
        ) == ("full", "7d")

    def test_classify_full_names_the_first_window_in_relevant_windows_order(self):
        """Both 5h and 7d blocking: name 5h, the order `relevant_windows`
        reports them in — deterministic, not the loudest one."""
        assert classify_candidate_block(
            [("5h", 95.0), ("7d", 92.0), ("Fable", 10.0)], 90.0
        ) == ("full", "5h")

    def test_the_decision_log_names_what_blocked_each_candidate(self):
        event = PollEvent(
            active={"number": 6, "email": "a@example.com"},
            headroom={"6": 28.0, "1": 9.0, "2": 40.0, "3": 60.0},
            threshold=90.0,
            windows={
                "1": {"5h": 34.0, "7d": 69.0, "Fable": 91.0},  # model-only
                "2": {"5h": 92.0, "7d": 10.0, "Fable": 20.0},  # full (5h)
                "3": {"5h": 5.0, "7d": 5.0, "Fable": 5.0},     # open
            },
        )
        text = event.human()
        assert "#1: 5h 34% · 7d 69% · Fable 91% (Fable-walled)" in text, text
        assert "#2: 5h 92% · 7d 10% · Fable 20% (5h full)" in text, text
        assert "#3: 5h 5% · 7d 5% · Fable 5%" in text, text
        assert "#3: 5h 5% · 7d 5% · Fable 5% (" not in text, text

    def test_a_spend_only_account_prints_its_credit_figure_not_a_bare_mark(
        self,
    ):
        """A credit (pay-as-you-go) account has no 5h/7d/model window at
        all -- `windows`/`headroom` are structurally empty for it, exactly
        the same shape `_describe` used to read as "genuinely unreadable"
        and print a bare ``?`` for. That collapses two different facts (no
        windows BY DESIGN vs. could-not-READ) into one mark, and it is the
        same contradiction the panel and this decision log showed on the
        same account: the panel prints the credit figure, this printed
        ``?``."""
        event = PollEvent(
            active={"number": 6, "email": "a@example.com"},
            headroom={"6": 28.0, "8": None},
            threshold=90.0,
            spend={"8": {"pct": 45.0, "used": 207.69, "limit": 466.0}},
        )
        text = event.human()
        assert "#8: ?" not in text, text
        assert "#8: $$ 45% ($207.69/$466.00)" in text, text

    def test_a_genuinely_unreadable_account_still_prints_a_bare_mark(self):
        """The discrimination must survive: an account with no windows, no
        spend figure and no named fetch error is still ``?`` -- backoff, a
        poll failure, a struck token. Never collapsed the other way either."""
        event = PollEvent(
            active={"number": 6, "email": "a@example.com"},
            headroom={"6": 28.0, "9": None},
            threshold=90.0,
        )
        text = event.human()
        assert "#9: ?" in text, text


class TestALiveSpecimenNeverLandsOnAnAccountThatIsItselfWalled:
    """The owner's own fleet (lmd42, 2026-09-08 ~01:2xZ, `dynamic`,
    threshold 90, Fable pinned): active #3 is Fable-100 (walled on the
    model axis), and #7 clears the Fable axis (10%) but is ITSELF over the
    departure threshold on its own 5h (91%) -- genuinely unusable, would
    re-trigger the very next tick. #2 is the only account below threshold
    on every axis (5h 40, 7d 65, Fable 89) and is the only correct target.
    1/5/6 are 7d-walled, #4 is Fable-walled with nothing else open.

    Reproduces the escape-axis ranking bug: `_rank_candidates_pass`'s
    at-limit escape ranked candidates by `escape_h` (headroom on the SAME
    axis that blocked the ACTIVE) alone, so #7's huge Fable headroom (90)
    beat #2's real, well-rounded headroom (11) even though #7's own
    binding window (5h) was already past the switch bar.
    """

    def _usage(self, now, **kw):
        def iso(days=0, hours=0, minutes=0):
            return _iso_at(now + days * 86400 + hours * 3600 + minutes * 60)

        return {
            "3": {  # active: Fable-walled, 5h/7d have room
                "five_hour": {"pct": 0.0},
                "seven_day": {"pct": 79.0, "resets_at": iso(days=3)},
                "scoped": [{"name": "Fable", "pct": 100.0, "resets_at": iso(days=6, hours=5)}],
            },
            "2": {  # the only account open on every axis
                "five_hour": {"pct": 40.0, "resets_at": iso(hours=2, minutes=33)},
                "seven_day": {"pct": 65.0, "resets_at": iso(days=6, hours=5)},
                "scoped": [{"name": "Fable", "pct": 89.0, "resets_at": iso(days=6, hours=5)}],
            },
            "7": {  # clear on Fable, but genuinely walled on its OWN 5h
                "five_hour": {"pct": 91.0, "resets_at": iso(hours=3, minutes=13)},
                "seven_day": {"pct": 18.0, "resets_at": iso(days=3, hours=20)},
                "scoped": [{"name": "Fable", "pct": 10.0}],
            },
            "1": {  # 7d-walled
                "five_hour": {"pct": 5.0}, "seven_day": {"pct": 100.0},
                "scoped": [{"name": "Fable", "pct": 5.0}],
            },
            "6": {  # 7d-walled
                "five_hour": {"pct": 5.0}, "seven_day": {"pct": 100.0},
                "scoped": [{"name": "Fable", "pct": 5.0}],
            },
            "5": {  # 7d-walled (and Fable-walled too)
                "five_hour": {"pct": 5.0}, "seven_day": {"pct": 100.0},
                "scoped": [{"name": "Fable", "pct": 100.0}],
            },
            "4": {  # Fable-walled, nothing else open
                "five_hour": {"pct": 5.0}, "seven_day": {"pct": 5.0},
                "scoped": [{"name": "Fable", "pct": 100.0}],
            },
        }

    def test_the_owners_fleet_switches_to_2_never_3_or_4(self, temp_home):
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num in (1, 2, 3, 4, 5, 6, 7):
            h.seed(num, f"acct{num}@example.invalid")
        h.make_live("acct3@example.invalid", 3)

        outcome = h.tick_with_usage(self._usage(h.clock.now))

        assert outcome is TickOutcome.SWITCHED, outcome
        assert h.active_number() == 2, (
            f"landed on {h.active_number()} — #2 is the only account below "
            "threshold on every axis; #7 clears only the axis that blocked "
            "the active and is itself over threshold on its own 5h, #3 is "
            "the active itself, #4 has nothing open but the model axis"
        )


class TestTheRetryAdmitsOnlyAnImprovement:
    """The 5h/7d retry (``_rank_candidates``'s second pass) must compare a
    landing to the ACTIVE on the retry's own axis, for every trigger shape
    — not only ``consume-first``. Without that, a candidate that is WORSE on
    5h/7d than the active still gets admitted, the engine switches onto it,
    the same model-gated trigger fires again next tick, and the fleet
    round-robins forever on candidates that were never an improvement.
    """

    @staticmethod
    def _usage_fable(fable_pct: float, five_h_pct: float) -> dict:
        return {
            "five_hour": {"pct": five_h_pct},
            "seven_day": {"pct": 0.0},
            "scoped": [{"name": "Fable", "pct": fable_pct}],
        }

    @staticmethod
    def _tick_loop(harness: EngineHarness, usage_map: dict, ticks: int) -> tuple[int, list]:
        switches = 0
        trace = []
        for _ in range(ticks):
            outcome = harness.tick_with_usage(usage_map)
            if outcome is TickOutcome.SWITCHED:
                switches += 1
            trace.append(harness.active_number())
            harness.clock.advance(301.0)  # past cooldown_seconds (300)
        return switches, trace

    def test_a_proactive_trigger_under_dynamic_never_churns_a_model_bar(
        self, temp_home
    ):
        """Every account is pinned-model-blocked at the same 95%, so every
        tick re-triggers a `proactive` retry on 5h/7d — under `dynamic`,
        the only strategy that ever reaches the retry. A landing must
        still beat the active by the hysteresis margin on the retry's own
        5h/7d axis; none of these candidates do (they hold LESS 5h
        headroom than whichever account is active), so nothing should
        ever move.
        """
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        usage_map = {
            "1": self._usage_fable(95.0, 10.0),  # 5h headroom 90
            "2": self._usage_fable(95.0, 20.0),  # 5h headroom 80
            "3": self._usage_fable(95.0, 30.0),  # 5h headroom 70
        }
        for num in usage_map:
            h.seed(int(num), f"acc{num}@example.com")
        h.make_live("acc1@example.com", 1)
        switches, trace = self._tick_loop(h, usage_map, ticks=8)
        assert switches == 0, (
            f"got {switches} switches, trace {trace} — every candidate is "
            "strictly worse on 5h/7d than whichever account is active; the "
            "retry must never admit a non-improvement"
        )

    def test_a_proactive_trigger_under_consume_first_skips_the_retry_and_holds(
        self, temp_home
    ):
        """SAME fleet as above, `consume-first` — today's deployed
        strategy. The retry is `dynamic`-only, so a model-gated pass that
        empties here never re-ranks on 5h/7d at all; the engine holds,
        exactly as it does before this PR, not because a landing
        comparison rejected a candidate."""
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="consume-first")
        usage_map = {
            "1": self._usage_fable(95.0, 10.0),
            "2": self._usage_fable(95.0, 20.0),
            "3": self._usage_fable(95.0, 30.0),
        }
        for num in usage_map:
            h.seed(int(num), f"acc{num}@example.com")
        h.make_live("acc1@example.com", 1)
        switches, trace = self._tick_loop(h, usage_map, ticks=8)
        assert switches == 0, (
            f"got {switches} switches, trace {trace} — consume-first must "
            "never reach the 5h/7d retry at all"
        )

    def test_an_at_limit_escape_under_dynamic_never_churns_a_model_bar(
        self, temp_home
    ):
        """Same shape, `dynamic` strategy, every account fully spent on the
        pinned model (`at-limit` trigger). The at-limit escape is allowed
        to skip the landing gate ONLY when the active is genuinely spent on
        the axis being ranked; on the 5h/7d retry axis here the active
        holds real headroom, so the escape must fall back to the ordinary
        hysteresis comparison — and none of these candidates clear it.
        """
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        usage_map = {
            "1": self._usage_fable(100.0, 10.0),  # 5h headroom 90
            "2": self._usage_fable(100.0, 20.0),  # 5h headroom 80
            "3": self._usage_fable(100.0, 30.0),  # 5h headroom 70
        }
        for num in usage_map:
            h.seed(int(num), f"acc{num}@example.com")
        h.make_live("acc1@example.com", 1)
        switches, trace = self._tick_loop(h, usage_map, ticks=8)
        assert switches == 0, (
            f"got {switches} switches, trace {trace} — the active holds "
            "real 5h/7d headroom on the retry's own axis, so the at-limit "
            "escape must not bypass the landing comparison"
        )

    def test_an_at_limit_escape_under_best_skips_the_retry_and_holds(
        self, temp_home
    ):
        """SAME fleet as above, `best` — the retry is `dynamic`-only, so
        `best`'s model-gated pass emptying here never re-ranks on 5h/7d at
        all; the engine holds exactly as it does before this PR."""
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="best")
        usage_map = {
            "1": self._usage_fable(100.0, 10.0),
            "2": self._usage_fable(100.0, 20.0),
            "3": self._usage_fable(100.0, 30.0),
        }
        for num in usage_map:
            h.seed(int(num), f"acc{num}@example.com")
        h.make_live("acc1@example.com", 1)
        switches, trace = self._tick_loop(h, usage_map, ticks=8)
        assert switches == 0, (
            f"got {switches} switches, trace {trace} — best must never "
            "reach the 5h/7d retry at all"
        )


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


class TestDynamicStrategy:
    """``dynamic``: consume-first's ranking key, the model basis re-picked
    each tick, and a landing rule that never lands on a candidate with no
    room on the axis in force — even when every account is above the
    threshold, where the pre-existing ``all_above`` recovery-axis escape
    otherwise admits one on a "recovers soonest" basis alone. Scoped to
    ``strategy == "dynamic"`` throughout: `best`/`consume-first` keep
    whatever they did before this class exists, and several tests below
    assert that directly, on the SAME inputs.
    """

    @staticmethod
    def _args(harness, *, usage, current, oauth_candidates, headroom,
              active_headroom, trigger, strategy, no_return=None):
        return dict(
            trigger=trigger,
            consume_first=strategy in ("consume-first", "dynamic"),
            no_return=no_return,
            oauth_candidates=oauth_candidates,
            usage=usage,
            headroom=headroom,
            current=current,
            active_headroom=active_headroom,
            settings=AutoSwitchSettings(threshold=90.0, strategy=strategy),
            now=harness.clock.now,
        )

    def test_the_voluntary_arm_never_lands_on_a_candidate_with_no_room(
        self, temp_home
    ):
        """Below the threshold, a candidate whose weekly window resets
        sooner than the active's is still not a landing once its own
        headroom (0.1) sits under the ordinary threshold bar."""
        h = EngineHarness(temp_home, strategy="dynamic")
        usage = {
            "1": _usage7(10.0, 20.0, _R_LATEST),   # active: headroom 90
            "2": _usage7(0.0, 99.9, _R_SOON),      # headroom 0.1, resets sooner
        }
        headroom = {"1": 90.0, "2": 0.1}
        args = self._args(
            h, usage=usage, current="1", oauth_candidates=["2"],
            headroom=headroom, active_headroom=90.0,
            trigger="dynamic", strategy="dynamic",
        )
        ordered, _, _, _ = h.engine._rank_candidates(**args)
        assert ordered == [], (
            f"got {ordered} — #2 is under the threshold (headroom 0.1) "
            "and must never be a landing no matter how soon its reset is"
        )

    def test_the_proactive_arm_never_lands_on_a_candidate_still_at_the_wall(
        self, temp_home
    ):
        """Reproduces the live trace: active over threshold with real
        headroom (`proactive`, not `at-limit`), every account above the
        threshold (`all_above`), and a peer whose OWN window is already at
        the switch threshold but whose binding window happens to reset
        soonest. The pre-existing `all_above` recovery-axis escape admits
        that peer on recovery timing alone — measured live, the very next
        tick read `cooldown` while still blocked. `dynamic` closes it;
        `best`/`consume-first` keep the escape (asserted on the SAME
        inputs, both directions, as the "unchanged" evidence)."""
        h = EngineHarness(temp_home, strategy="dynamic")
        now = h.clock.now
        usage = {
            "6": {  # active: headroom 8, recovers in ~5.5h
                "five_hour": {"pct": 92.0, "resets_at": _iso_at(now + 20000)},
                "seven_day": {"pct": 0.0},
            },
            "2": {  # candidate: headroom 10 (at the wall), recovers in 2min
                "five_hour": {"pct": 90.0, "resets_at": _iso_at(now + 120)},
                "seven_day": {"pct": 0.0},
            },
        }
        headroom = {"6": 8.0, "2": 10.0}
        for strategy, expected in (
            ("dynamic", []),
            ("consume-first", ["2"]),
            ("best", ["2"]),
        ):
            args = self._args(
                h, usage=usage, current="6", oauth_candidates=["2"],
                headroom=headroom, active_headroom=8.0,
                trigger="proactive", strategy=strategy,
            )
            ordered, _, _, _ = h.engine._rank_candidates(**args)
            assert ordered == expected, (
                f"strategy={strategy}: got {ordered}, want {expected} — #2 "
                "is still at the wall (headroom 10, threshold 90) and only "
                "`dynamic` may refuse to land there"
            )

    def test_consume_first_switches_on_a_plain_proactive_trigger_dynamic_holds(
        self, temp_home
    ):
        """No `--model`, primary pass. Active at 91% (over the 90 threshold,
        `proactive`, headroom 9); one healthy candidate at 85% (headroom
        15) — 6 points better, short of the default 10-point
        `hysteresis_pct`. `consume-first` must switch here exactly as it
        does today (its base behaviour on this trigger is unconditional
        admission, restored on the owner's word — a real anti-flap gate
        for it is a separate, authorized round); `dynamic` holds, gated by
        the ordinary hysteresis margin like `best`."""
        h = EngineHarness(temp_home, strategy="dynamic")
        now = h.clock.now
        usage = {
            "1": {  # active: headroom 9, over threshold
                "five_hour": {"pct": 91.0, "resets_at": _iso_at(now + 20000)},
                "seven_day": {"pct": 0.0},
            },
            "2": {  # candidate: headroom 15, healthy but < hysteresis margin
                "five_hour": {"pct": 85.0, "resets_at": _iso_at(now + 90000)},
                "seven_day": {"pct": 0.0},
            },
        }
        headroom = {"1": 9.0, "2": 15.0}
        for strategy, expected in (
            ("consume-first", ["2"]),
            ("dynamic", []),
        ):
            args = self._args(
                h, usage=usage, current="1", oauth_candidates=["2"],
                headroom=headroom, active_headroom=9.0,
                trigger="proactive", strategy=strategy,
            )
            ordered, _, _, _ = h.engine._rank_candidates(**args)
            assert ordered == expected, (
                f"strategy={strategy}: got {ordered}, want {expected} — "
                "#2 beats the active by 6 points, under the 10-point "
                "hysteresis margin"
            )

    def test_the_at_limit_arm_never_lands_on_a_candidate_still_at_the_wall(
        self, temp_home
    ):
        """Same shape as the proactive case, `at-limit` trigger with the
        active NOT `about_to_wall` on this axis (the 5h/7d retry pass,
        where the active can hold real headroom even though the model gate
        that set the trigger spent it) — the other trigger the `all_above`
        recovery axis reaches."""
        h = EngineHarness(temp_home, strategy="dynamic")
        now = h.clock.now
        usage = {
            "6": {
                "five_hour": {"pct": 92.0, "resets_at": _iso_at(now + 20000)},
                "seven_day": {"pct": 0.0},
            },
            "2": {
                "five_hour": {"pct": 90.0, "resets_at": _iso_at(now + 120)},
                "seven_day": {"pct": 0.0},
            },
        }
        headroom = {"6": 8.0, "2": 10.0}
        for strategy, expected in (
            ("dynamic", []),
            ("consume-first", ["2"]),
        ):
            args = self._args(
                h, usage=usage, current="6", oauth_candidates=["2"],
                headroom=headroom, active_headroom=8.0,
                trigger="at-limit", strategy=strategy,
            )
            ordered, _, _, _ = h.engine._rank_candidates(**args)
            assert ordered == expected, (
                f"strategy={strategy}: got {ordered}, want {expected} — the "
                "at-limit escape must not bypass the landing bar for "
                "dynamic just because `about_to_wall` is false on this axis"
            )

    def test_the_at_limit_escape_still_lands_when_the_active_is_genuinely_spent(
        self, temp_home
    ):
        """The primary at-limit arm, deliberately UNCHANGED: when the
        active is genuinely spent on the axis in force (`about_to_wall`),
        the escape still lands on a candidate with real headroom even
        though every candidate here is ALSO over the switch threshold
        (`all_above` holds too). Sitting on a zero-headroom account serves
        nothing; a candidate at 92% still serves 8%. The reviewer proposed
        extending the landing gate to this arm; the owner ruled against it
        — this pins the refusal, not a gap."""
        h = EngineHarness(temp_home, strategy="dynamic")
        now = h.clock.now
        usage = {
            "1": {  # active: fully spent
                "five_hour": {"pct": 100.0, "resets_at": _iso_at(now + 20000)},
                "seven_day": {"pct": 0.0},
            },
            "2": {  # candidate: over the switch threshold, not spent
                "five_hour": {"pct": 92.0, "resets_at": _iso_at(now + 300)},
                "seven_day": {"pct": 0.0},
            },
        }
        headroom = {"1": 0.0, "2": 8.0}
        args = self._args(
            h, usage=usage, current="1", oauth_candidates=["2"],
            headroom=headroom, active_headroom=0.0,
            trigger="at-limit", strategy="dynamic",
        )
        ordered, _, _, _ = h.engine._rank_candidates(**args)
        assert ordered == ["2"], (
            f"got {ordered} — the active holds no headroom at all; the "
            "at-limit escape must still land on #2's real 8 points even "
            "though #2 is itself over the 90 threshold"
        )

    def test_fleet_churn_a_reset_driven_departure_does_not_dominance_release(
        self, temp_home
    ):
        """`_left_account_recovered`'s dominance leg releases the no-return
        bar when the barred account now beats the CURRENT active by a wide
        margin — correct when we left for headroom reasons, but a
        `consume-first`/`dynamic` departure leaves for RESET ordering, so
        the barred account can dominate the very account we moved to from
        the moment we left (its headroom never needed to move). Measured:
        re-testing that same, unchanged dominance every tick released the
        bar every tick and the reset-ordering key sent the engine straight
        back — two accounts ping-ponging while neither ever changed.
        `dynamic` requires a REAL improvement against the departure
        baseline instead (the two legs below this one); `best`/
        `consume-first` keep the bare dominance leg, asserted here on the
        SAME state."""
        h = EngineHarness(temp_home, strategy="dynamic")
        now = h.clock.now
        state = {
            "lastSwitchFrom": "5",
            "leftHeadroom": 50.0,
            "leftRecoveryAt": now + 50000,
            "leftTrigger": "dynamic",
        }
        usage = {
            # The barred account: IDENTICAL to its departure snapshot —
            # same headroom, same reset — only the active changed.
            "5": {
                "five_hour": {"pct": 50.0, "resets_at": _iso_at(now + 50000)},
                "seven_day": {"pct": 0.0},
            },
            "2": {
                "five_hour": {"pct": 80.0, "resets_at": _iso_at(now + 90000)},
                "seven_day": {"pct": 0.0},
            },
        }
        headroom = {"5": 50.0, "2": 20.0}
        recovered_dynamic = h.engine._left_account_recovered(
            state, usage, headroom, 20.0,
            AutoSwitchSettings(threshold=90.0, strategy="dynamic"),
            now, current="2",
        )
        recovered_consume_first = h.engine._left_account_recovered(
            state, usage, headroom, 20.0,
            AutoSwitchSettings(threshold=90.0, strategy="consume-first"),
            now, current="2",
        )
        assert recovered_consume_first is True, (
            "consume-first must be unchanged: bare dominance over the "
            "current active still releases the bar"
        )
        assert recovered_dynamic is False, (
            "dynamic must hold: #5 is byte-identical to when we left it, "
            "so re-measuring the same dominance is not an improvement"
        )

    def test_a_forced_threshold_return_still_dominance_releases_under_dynamic(
        self, temp_home
    ):
        """The fleet-churn suppression above guards a VOLUNTARY reset-ordering
        return (nobody had to move). It must not also hold when the ACTIVE
        has itself burned down to the switch threshold: at that point a
        return is forced, same as an ordinary departure, and hiding the best
        candidate just delays the correct switch behind extra cooldown
        ticks. Exact numbers from a reproduced live trace: #1 was left at
        62 pts on a `dynamic`-trigger preference departure (reset ordering,
        not headroom), then #2 (the account we moved to) burned down to the
        90% threshold while #1 sat essentially unchanged at 61 pts — #1
        should be released and re-admitted as a candidate."""
        h = EngineHarness(temp_home, strategy="dynamic")
        now = h.clock.now
        state = {
            "lastSwitchFrom": "1",
            "lastSwitchTo": "2",
            "leftHeadroom": 62.0,
            "leftRecoveryAt": now + 9_000_000,
            "leftTrigger": "dynamic",
        }
        usage = {
            "1": {
                "five_hour": {"pct": 39.0, "resets_at": _iso_at(now + 9_000_000)},
                "seven_day": {"pct": 0.0},
            },
            "2": {
                "five_hour": {"pct": 90.0, "resets_at": _iso_at(now + 300)},
                "seven_day": {"pct": 0.0},
            },
        }
        headroom = {"1": 61.0, "2": 10.0}
        settings = AutoSwitchSettings(threshold=90.0, strategy="dynamic")
        recovered = h.engine._left_account_recovered(
            state, usage, headroom, 10.0, settings, now, current="2",
        )
        assert recovered is True, (
            "the active is AT the 90% threshold (10 pts headroom left) — "
            "this is a forced return, not the voluntary reset-ordering "
            "ping-pong the fleet-churn leg guards against, so #1's "
            "dominance over the active must still release the bar"
        )
        no_return = h.engine._no_return_account(
            "proactive", state, headroom, 10.0, recovered, settings, current="2",
        )
        assert no_return is None, (
            f"got no_return={no_return!r} — a released bar must also clear "
            "the ranking loop's exclusion, or the proactive arm still "
            "cannot land on #1"
        )

    # -- the owner's live acceptance fixture, exact numbers ------------------
    #
    #   acct   5h    7d   Fable   note
    #     5     0    62     90    7d reset soonest, ~14h
    #     4     0    64     91
    #     3    33    69     94
    #     1    34    69     91
    #     2    94     —      —    5h at the wall; nothing can use it
    #     6    90     —      —    5h at the wall
    #
    # Placeholder emails throughout — none of the owner's real addresses.

    @staticmethod
    def _owner_fleet(now: float) -> dict:
        def acct(five_h, seven_d, fable, hours_out):
            return {
                "five_hour": {"pct": five_h},
                "seven_day": {
                    "pct": seven_d, "resets_at": _iso_at(now + hours_out * 3600),
                },
                "scoped": [{"name": "Fable", "pct": fable}],
            }

        return {
            "5": acct(0, 62, 90, 14),
            "4": acct(0, 64, 91, 20),
            "3": acct(33, 69, 94, 40),
            "1": acct(34, 69, 91, 60),
            "2": {"five_hour": {"pct": 94.0}},
            "6": {"five_hour": {"pct": 90.0}},
        }

    def _owner_harness(self, temp_home, *, active_num: int) -> EngineHarness:
        h = EngineHarness(
            temp_home, model="Fable", threshold=90.0, strategy="dynamic",
        )
        order = [active_num] + [n for n in (1, 2, 3, 4, 5, 6) if n != active_num]
        for num in order:
            h.seed(num, f"acct{num}@example.invalid")
        h.make_live(f"acct{active_num}@example.invalid", active_num)
        return h

    def test_owner_fixture_no_departure_from_account_5(self, temp_home):
        """Account 5's Fable window sits at the switch threshold (90), but
        its 5h/7d have room. Ten ticks, static usage (nothing genuinely
        changes): zero switches — held by a DIFFERENT guard than the one
        this test used to exercise, and that is the point of the update
        below.

        The owner's six accounts alone do not DISCRIMINATE the re-pick:
        every one of them is also blocked on the model-gated axis (>=90),
        so the landing gate refuses them whether or not the re-pick ran.
        Account 7 (added here, not one of the owner's six) is open and
        healthy on the model-gated axis — which is exactly why, post-fix,
        the re-pick must NOT widen account 5 here: `_model_window_binds_
        everywhere` is false the moment any account (7) is open with the
        model folded in, so widening account 5 past its real 10%-headroom
        Fable reading would be calling a genuinely near-walled active
        healthy on the strength of an unrelated account's headroom — the
        exact bug this round fixed (an active pinned on a real wall while
        a real Fable-healthy candidate sat idle). Account 5 still does not
        depart, but now because account 7 is COLD (never active) and its
        model-gated headroom (25) does not clear `cold_switch_cost_pct`
        (#375's own floor, orthogonal to this fix) — not because the
        re-pick manufactured a healthier reading for account 5.
        """
        from claude_swap.autoswitch import _dynamic_active_headroom

        h = self._owner_harness(temp_home, active_num=5)
        h.seed(7, "acct7@example.invalid")
        fleet_usage = dict(self._owner_fleet(h.clock.now))
        fleet_usage["7"] = {
            "five_hour": {"pct": 0.0},
            "seven_day": {
                "pct": 0.0,
                "resets_at": _iso_at(h.clock.now + 100 * 3600),
            },
            "scoped": [{"name": "Fable", "pct": 75.0}],
        }

        # The active_headroom the tick actually uses: UNCHANGED at the
        # model-gated 10.0 — account 7's real Fable headroom means the
        # model window does not bind everywhere, so the re-pick must leave
        # account 5's reading alone rather than widening it to the
        # unmodeled 38.
        widened = _dynamic_active_headroom(
            h.engine.settings, h.engine._models, fleet_usage, "5", 10.0,
        )
        assert widened == 10.0, (
            f"got {widened!r} — account 7 is open on the model-gated axis, "
            "so the window does not bind everywhere and account 5's real "
            "10% Fable headroom must stand, not be widened to 38"
        )

        switches = 0
        reasons = []
        for _ in range(10):
            h.events.clear()
            outcome = h.tick_with_usage(fleet_usage)
            if outcome is TickOutcome.SWITCHED:
                switches += 1
            else:
                reasons.extend(
                    e.reason for e in h.events if isinstance(e, NoSwitchEvent)
                )
            h.clock.advance(301.0)  # past cooldown_seconds (300, the default)
        assert switches == 0, (
            f"got {switches} switches off account 5 — its Fable window "
            "must not force a departure while 5h/7d have room, and account "
            "7's later-resetting weekly window must not admit it either"
        )
        assert h.active_number() == 5
        # Every hold reads as `cold` (#375: `dynamic`'s proactive arm needs
        # `about_to_wall` (SPENT_HEADROOM_PCT=3.0), and account 5's
        # headroom (10, no longer widened by this round's fix) is nowhere
        # near it either; account 7 is open but never active, so it lands
        # cold and does not clear `cold_switch_cost_pct`) — never the
        # plain-hysteresis "proactive" a fully-dropped re-pick would have
        # taken (which would have switched to 7, not held).
        assert all(r == "cold" for r in reasons), reasons

    def test_owner_fixture_holds_on_account_2_with_no_warm_partner(self, temp_home):
        """#375 superseded this fixture's premise: starting on account 2
        (headroom 6) used to land on account 5 by soonest-7d-reset
        ordering while account 2 was merely below the ordinary threshold,
        never genuinely `about_to_wall` (headroom 6 > SPENT_HEADROOM_PCT's
        3). Under #375 `dynamic`'s proactive arm does not fire there, and
        no candidate is a warm alternation partner (none has ever been
        active) — so the engine now holds on account 2."""
        h = self._owner_harness(temp_home, active_num=2)
        fleet_usage = self._owner_fleet(h.clock.now)
        outcome = h.tick_with_usage(fleet_usage)
        assert outcome is TickOutcome.NO_ACTION, (
            f"got {outcome} — account 2's headroom (6) is not "
            "`about_to_wall`, so #375's `dynamic` must not move"
        )
        assert h.active_number() == 2

    def test_the_model_basis_re_pick_is_what_makes_the_trigger_voluntary(
        self, temp_home
    ):
        """The single-account shape that tells the re-pick apart from the
        landing rule alone: active blocked ONLY on Fable (5h/7d have real
        room), no OTHER account in rotation at all. Without re-picking the
        basis, the trigger reads `proactive` (the raw, model-gated headroom
        is at the threshold) and an engine with zero candidates takes the
        generic `no-candidates` / BLOCKED path. Re-picking the basis first
        finds 5h/7d has room, so the trigger is the ordinary below-threshold
        `dynamic` one — and THAT path's own "no OAuth peer to compare
        against" branch answers `below-threshold` / NO_ACTION instead,
        before candidate selection is ever reached. Same event either way
        that "nothing switched" — the reason and exit code are what only
        the re-pick gets right."""
        h = EngineHarness(
            temp_home, model="Fable", threshold=90.0, strategy="dynamic",
        )
        h.seed(5, "acct5@example.invalid")
        h.make_live("acct5@example.invalid", 5)
        outcome = h.tick_with_usage({
            "5": {
                "five_hour": {"pct": 0.0},
                "seven_day": {"pct": 62.0},
                "scoped": [{"name": "Fable", "pct": 90.0}],
            },
        })
        assert outcome is TickOutcome.NO_ACTION, (
            f"got {outcome} — the model basis must be re-picked BEFORE the "
            "no-candidates check, or a lone account blocked only on its "
            "model window reads as genuinely blocked (BLOCKED, not "
            "NO_ACTION)"
        )
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"], (
            f"got {reasons} — without the re-pick this reads `no-candidates`"
        )

    def test_primary_pass_ranks_active_and_candidate_headroom_on_one_basis(
        self, temp_home
    ):
        """#375 replaced dynamic's `proactive`-arm admission with an
        ABSOLUTE floor (`cold_switch_cost_pct`) instead of a hysteresis
        MARGIN against the active — so this is no longer a "which basis"
        question (the floor does not read `active_headroom` at all). What
        it still pins: the floor itself must read the SAME axis the retry
        rescued a candidate on. Active #1 is blocked on Fable alone
        (model-gated headroom 1, widened/unmodeled 3 -- `about_to_wall`).
        #2 and #3 are both healthy candidates that clear the floor (25 and
        30); #3 resets far sooner (5h vs 60h) and must win.
        """
        h = EngineHarness(
            temp_home, model="Fable", threshold=90.0, strategy="dynamic",
        )
        h.seed(1, "acct1@example.invalid")
        h.seed(2, "acct2@example.invalid")
        h.seed(3, "acct3@example.invalid")
        h.make_live("acct1@example.invalid", 1)

        def acct(five_h, seven_d, fable, hours_out):
            return {
                "five_hour": {"pct": five_h},
                "seven_day": {
                    "pct": seven_d,
                    "resets_at": _iso_at(h.clock.now + hours_out * 3600),
                },
                "scoped": [{"name": "Fable", "pct": fable}],
            }

        outcome = h.tick_with_usage({
            "1": acct(50, 97, 99, 30),
            "2": acct(10, 10, 75, 60),
            "3": acct(10, 10, 70, 5),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3, (
            f"landed on {h.active_number()} instead of account 3 — the "
            "soonest-resetting candidate that clears the floor"
        )


class TestWarmthAndAlternation375:
    """#375 (event #375, owner 2026-09-07): `dynamic` drops the bare
    threshold (its proactive arm now needs `about_to_wall`) and gains a
    reset-TTL-aware, warm-preferred admission (item 3) plus a healthy-
    active alternation arm (item 4) so a healthy fleet spends quota by
    rotating between accounts whose cached context is still warm, rather
    than by chasing whichever weekly window resets soonest regardless of
    cost.
    """

    def _harness(self, temp_home, **kwargs):
        h = EngineHarness(temp_home, strategy="dynamic", **kwargs)
        h.seed(1, "acct1@example.invalid")
        h.seed(2, "acct2@example.invalid")
        h.seed(3, "acct3@example.invalid")
        h.make_live("acct1@example.invalid", 1)
        return h

    @staticmethod
    def _seed_last_active_at(h, mapping):
        path = h.switcher.backup_dir / "autoswitch_state.json"
        raw = json.loads(path.read_text()) if path.exists() else {"schemaVersion": 1}
        raw["lastActiveAt"] = mapping
        path.write_text(json.dumps(raw))

    # -- F1: warm-preferred admission ----------------------------------

    def test_f1_warm_ranks_ahead_of_a_higher_headroom_cold_candidate(
        self, temp_home
    ):
        h = self._harness(temp_home)
        self._seed_last_active_at(h, {"2": h.clock.now - 600.0})
        outcome = h.tick_with_usage({
            "1": _usage(97.0),  # active, about_to_wall (headroom 3)
            "2": _usage(90.0),  # warm, headroom 10 -- less
            "3": _usage(10.0),  # cold, headroom 90 -- more, clears the floor
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2, (
            f"landed on {h.active_number()} — the warm candidate (less "
            "headroom) must rank ahead of the cold one (more headroom)"
        )

    # -- F2: cold refused until the active is walled --------------------

    def test_f2_healthy_active_cold_candidate_below_floor_refused(
        self, temp_home
    ):
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage(78.0, resets_at=None) | {"seven_day": {"pct": 56.0}},
            "2": _usage(96.0),  # cold, headroom 4 -- below the floor (20)
        })
        assert outcome is TickOutcome.NO_ACTION
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-floor"], reasons
        assert h.active_number() == 1

    def test_f2_healthy_active_cold_candidate_above_floor_still_refused(
        self, temp_home
    ):
        """Active not `about_to_wall` -- clearing the floor is not enough;
        the reason is `cold` (refused: active not walled), distinct from
        `below-floor` (refused: doesn't even clear the floor)."""
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage(78.0) | {"seven_day": {"pct": 56.0}},
            "2": _usage(50.0),  # cold, headroom 50 -- clears the floor easily
        })
        assert outcome is TickOutcome.NO_ACTION
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["cold"], reasons
        assert h.active_number() == 1

    def test_m1_dynamic_healthy_hold_detail_has_no_false_comparison(
        self, temp_home
    ):
        """m1: utilization (95) can sit ABOVE `settings.threshold` (90)
        here -- the old `f"{utilization}% < {threshold}%"` detail printed
        a false "95% < 90%". No comparison glyph; state what the hold
        means instead."""
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage(95.0),  # active, headroom 5 -- healthy, > threshold
            "2": _usage(94.0),  # cold, headroom 6 -- below the floor
        })
        assert outcome is TickOutcome.NO_ACTION
        detail = next(
            e.detail for e in h.events if isinstance(e, NoSwitchEvent)
        )
        assert "<" not in detail, detail
        assert "healthy" in detail, detail

    def test_f2_walled_active_admits_the_cold_candidate_above_floor(
        self, temp_home
    ):
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage(98.0) | {"seven_day": {"pct": 0.0}},  # headroom 2
            "2": _usage(50.0),  # cold, headroom 50
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_f2_walled_active_refuses_the_cold_candidate_below_floor(
        self, temp_home
    ):
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage(98.0) | {"seven_day": {"pct": 0.0}},  # headroom 2
            "2": _usage(96.0),  # cold, headroom 4 -- below the floor
        })
        assert outcome is TickOutcome.NO_ACTION
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-floor"], reasons
        assert h.active_number() == 1

    def test_f2_at_limit_escapes_to_a_below_floor_candidate(self, temp_home):
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage(100.0) | {"seven_day": {"pct": 0.0}},  # headroom 0
            "2": _usage(96.0),  # cold, headroom 4 -- below the floor
        })
        assert outcome is TickOutcome.SWITCHED
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "at-limit"
        assert h.active_number() == 2

    # -- F3: alternation fires at the chunk boundary and returns ---------

    def test_f3_alternation_fires_at_the_chunk_boundary_and_returns(
        self, temp_home
    ):
        h = self._harness(temp_home)
        chunk = h.engine.settings.alternation_chunk_seconds
        start = h.clock.now
        self._seed_last_active_at(h, {
            "1": start - (chunk - 1.0),
            "2": start - 10.0,  # warm, real headroom
        })
        # Within one `ALTERNATION_MAX_GIVEBACK_PCT` of each other in BOTH
        # directions --
        # the round trip this test is about is only alternation while it
        # gives nothing material back (#321 follow-up). The old pair (50
        # vs 70) handed back 20 points on the return leg, which the
        # giveback bar now refuses; the engine consumes the richer account
        # until the two converge and alternation resumes.
        usage = {"1": _usage(50.0), "2": _usage(45.0)}

        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.NO_ACTION, f"got {outcome} at chunk - 1s"
        assert h.active_number() == 1

        h.clock.advance(1.0)  # now exactly at the chunk boundary
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "alternation", sw.trigger
        assert h.active_number() == 2

        state = h.state()
        assert state["lastActiveAt"]["1"] == h.clock.now, (
            "the departed account must be stamped warm on departure"
        )

        h.clock.advance(chunk)
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome} — must alternate back once the next chunk "
            "elapses on the still-warm departed account"
        )
        assert h.active_number() == 1

    def test_an_untrustworthy_partner_does_not_strand_the_tick(self, temp_home):
        """I1's mirror on the ordinary (non-escape) alternation path:
        `dynamic_ordered` used to be `[partner]` alone, so a struck top-
        ranked warm partner stranded the tick instead of falling through
        to the next floor-clearing warm candidate. #2 outranks #3 (more
        headroom, same untiered 7d reset) so it is `partner`; struck this
        time — #3 must still be reached.
        """
        h = self._harness(temp_home)
        chunk = h.engine.settings.alternation_chunk_seconds
        self._seed_last_active_at(h, {
            "1": h.clock.now - chunk,
            "2": h.clock.now - 10.0,
            "3": h.clock.now - 10.0,
        })
        usage = {"1": _usage(50.0), "2": _usage(30.0), "3": _usage(60.0)}
        entries = {num: _entry_for(value, h.clock.now) for num, value in usage.items()}
        entries["2"] = UsageEntry(
            last_good=usage["2"], fetched_at=h.clock.now, age_s=0.0,
            auth_dead_strikes=2,
        )
        assert entries["2"].token_dead()
        outcome = h.tick_with_entries(entries)
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome!r} — a struck top partner (#2) must not "
            "strand the tick while a healthy, lower-ranked one (#3) is "
            "available"
        )
        assert h.active_number() == 3

    # -- F4: TTL expiry makes a partner cold -----------------------------

    def test_f4_a_partner_exactly_at_the_ttl_boundary_is_cold(self, temp_home):
        """m3: pin `_is_warm`'s `<`, not `<=` -- exactly `now - ttl` reads
        cold. A mutant flipping the comparison must fail this."""
        h = self._harness(temp_home)
        ttl = h.engine.settings.cache_ttl_seconds
        chunk = h.engine.settings.alternation_chunk_seconds
        self._seed_last_active_at(h, {
            "1": h.clock.now - chunk,   # past the chunk boundary already
            "2": h.clock.now - ttl,     # exactly at the TTL -- cold
        })
        outcome = h.tick_with_usage({"1": _usage(50.0), "2": _usage(30.0)})
        assert outcome is TickOutcome.NO_ACTION, (
            f"got {outcome} — a partner exactly at the TTL boundary is "
            "cold, never a warm alternation pick"
        )
        assert h.active_number() == 1

    def test_f4_a_partner_one_second_inside_the_ttl_is_warm(self, temp_home):
        """m3's other half: one second inside the boundary must still be
        warm -- pins the same `<` from the other side."""
        h = self._harness(temp_home)
        ttl = h.engine.settings.cache_ttl_seconds
        chunk = h.engine.settings.alternation_chunk_seconds
        self._seed_last_active_at(h, {
            "1": h.clock.now - chunk,       # past the chunk boundary already
            "2": h.clock.now - ttl + 1.0,   # one second inside -- warm
        })
        outcome = h.tick_with_usage({"1": _usage(50.0), "2": _usage(30.0)})
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome} — a partner one second inside the TTL must "
            "still be a warm alternation pick"
        )
        assert h.active_number() == 2

    # -- no-return bar on the proactive/about_to_wall arm -----------------

    def test_no_return_bar_blocks_a_spent_peer_even_when_its_reset_recovered(
        self, temp_home
    ):
        """The fable reviewer's flap: A (h=3) -> B (warm, h=10); B burns to
        h=3; A's own headroom never moves (still 2, spent) but its BINDING
        RESET alone reads much closer than it did at departure --
        `_left_account_recovered`'s reset leg alone says "recovered" even
        though headroom says nothing changed. Without the warm floor
        (`h > SPENT_HEADROOM_PCT`, not `h > 0`) the no-return bar's own
        "barred list is empty and recovered -- retry unbarred" fallback
        re-admits A anyway (the mutant control below). NOT `_usage()`'s
        bare form for either tick: the reset comparison needs the SAME (5h)
        window closer at tick 2 than at tick 1, and `_usage()` alone
        reports no reset at all.

        I1: with the warm floor intact, A (h=2) is genuinely inadmissible
        even fully unbarred -- both accounts are now exhausted, so this
        falls through to the shared exhausted-fleet handling (BLOCKED),
        not the bar's own NO_ACTION hold."""
        h = self._harness(temp_home)
        self._seed_last_active_at(h, {"2": h.clock.now - 10.0})
        far = _iso_at(h.clock.now + 500 * 3600)
        h.tick_with_usage({
            "1": _usage(97.0, resets_at=far),    # active, about_to_wall (h=3)
            "2": _usage(90.0),                    # warm, h=10
        })
        assert h.active_number() == 2, "setup: must have switched 1 -> 2"

        h.clock.advance(301.0)  # past cooldown
        near = _iso_at(h.clock.now + 3600.0)  # much closer than `far` was
        outcome = h.tick_with_usage({
            "2": _usage(97.0),                    # active, about_to_wall (h=3)
            "1": _usage(98.0, resets_at=near),    # h=2 -- still spent
        })
        assert outcome is TickOutcome.BLOCKED, (
            f"got {outcome} — account 1 is still spent (h=2); its reset "
            "'recovering' alone must not re-admit it, and with nothing "
            "else viable this is I1's exhausted-fleet fallthrough"
        )
        assert h.active_number() == 2

    def test_no_return_bar_blocks_a_return_that_has_not_recovered(
        self, temp_home
    ):
        """Same shape, but account 1's headroom (5, past the warm floor) is
        what could wrongly look like enough on its own -- the bar must
        still hold it because NOTHING about it actually improved since
        departure (no reset info at all on either tick, so the reset leg
        cannot release it either)."""
        h = self._harness(temp_home)
        self._seed_last_active_at(h, {"2": h.clock.now - 10.0})
        h.tick_with_usage({
            "1": _usage(97.0),   # active, about_to_wall (h=3)
            "2": _usage(90.0),   # warm, h=10
        })
        assert h.active_number() == 2, "setup: must have switched 1 -> 2"

        h.clock.advance(301.0)  # past cooldown
        outcome = h.tick_with_usage({
            "2": _usage(97.0),   # active, about_to_wall (h=3)
            "1": _usage(95.0),   # h=5 -- clears the warm floor, unrecovered
        })
        assert outcome is TickOutcome.NO_ACTION, (
            f"got {outcome} — account 1 clears the warm floor but has not "
            "recovered; the no-return bar must still hold it"
        )
        assert h.active_number() == 2

    def test_at_limit_escapes_the_no_return_bar_once(self, temp_home):
        """`_no_return_account` scopes `at-limit` out by design (unchanged):
        once the active is genuinely spent, the bar must not strand the
        engine on it just because the only candidate is the one it left."""
        h = self._harness(temp_home)
        self._seed_last_active_at(h, {"2": h.clock.now - 10.0})
        h.tick_with_usage({
            "1": _usage(97.0),
            "2": _usage(90.0),
        })
        assert h.active_number() == 2, "setup: must have switched 1 -> 2"

        h.clock.advance(301.0)
        h.events.clear()
        outcome = h.tick_with_usage({
            "2": _usage(100.0),  # active, fully spent -- at-limit
            "1": _usage(95.0),   # the barred account, unrecovered
        })
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome} — at-limit must escape even to the barred "
            "account once the active is genuinely spent"
        )
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "at-limit", sw.trigger
        assert h.active_number() == 1

    # -- I1: a genuinely exhausted fleet falls through, not NO_ACTION forever --

    def test_i1_a_genuinely_exhausted_fleet_falls_through_to_blocked_and_sleeps(
        self, temp_home
    ):
        """No prior switch (`no_return is None`) -- the proactive arm's
        own "nothing cleared even the warm floor" case must fall through
        to the shared exhausted-fleet handling (BLOCKED, the sleep armed),
        exactly as best/consume-first do, not return NO_ACTION forever."""
        h = self._harness(temp_home)
        soon = _iso_at(h.clock.now + 3600.0)
        outcome = h.tick_with_usage({
            "1": _usage(98.0, resets_at=soon),   # active, about_to_wall (h=2)
            "2": _usage(100.0, resets_at=soon),  # candidate, truly exhausted
            "3": _usage(100.0, resets_at=soon),
        })
        assert outcome is TickOutcome.BLOCKED, (
            f"got {outcome} — a genuinely exhausted fleet must fall "
            "through to BLOCKED, not return NO_ACTION early"
        )
        assert h.engine._sleep_until_ts is not None, (
            "the earliest-reset sleep must be armed, exactly as best/"
            "consume-first do on a truly exhausted fleet"
        )
        assert h.active_number() == 1

    def test_i1_the_api_key_last_resort_is_reached_when_configured(
        self, temp_home
    ):
        h = EngineHarness(
            temp_home, strategy="dynamic", include_api_key_accounts=True,
        )
        h.seed(1, "acct1@example.invalid")
        h.seed(2, "key@token.local")
        h.make_live("acct1@example.invalid", 1)
        data = h.switcher._get_sequence_data()
        data["accounts"]["2"]["kind"] = "api_key"
        h.switcher._write_json(h.switcher.sequence_file, data)

        outcome = h.tick_with_usage({
            "1": _usage(98.0),  # active, about_to_wall (h=2), no oauth peer
            "2": "api key",
        })
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome} — with no oauth candidate at all, the api-key "
            "last resort must be reached exactly as best/consume-first do"
        )
        assert h.active_number() == 2

    # -- I2: the incoming stamp is alternation's own chunk-timer origin --

    def test_i2_the_incoming_stamp_gates_alternations_own_chunk_timer(
        self, temp_home
    ):
        """A landing target for a PROACTIVE switch can already be warm
        from an earlier cycle -- arrival must OVERWRITE that stale
        `lastActiveAt` entry with the fresh arrival time, or alternation's
        own chunk timer reads it as dwelt-on far longer than it has been,
        and fires on the very next post-cooldown tick instead of waiting a
        full chunk."""
        h = self._harness(temp_home)
        chunk = h.engine.settings.alternation_chunk_seconds
        start = h.clock.now
        # "2" warm from an earlier cycle -- stale enough that, left
        # un-overwritten, `now - since` clears a full chunk within one
        # cooldown of arriving.
        self._seed_last_active_at(h, {"2": start - (chunk - 250.0)})

        outcome = h.tick_with_usage({
            "1": _usage(98.0),  # active, about_to_wall (h=2)
            "2": _usage(50.0),  # warm, real headroom -- proactive landing
        })
        assert outcome is TickOutcome.SWITCHED
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "proactive", sw.trigger
        assert h.active_number() == 2

        h.clock.advance(301.0)  # past cooldown, short of a full chunk
        h.events.clear()
        outcome = h.tick_with_usage({
            "2": _usage(50.0),  # active, healthy -- dynamic-healthy now
            "1": _usage(30.0),  # warm partner (just departed)
        })
        assert outcome is TickOutcome.NO_ACTION, (
            f"got {outcome} — the incoming stamp must reset 2's dwell "
            "clock on arrival; alternation must not fire before a full "
            "chunk has elapsed since the actual arrival"
        )

        h.clock.advance(chunk - 301.0)
        outcome = h.tick_with_usage({"2": _usage(50.0), "1": _usage(30.0)})
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome} — alternation must fire once a full chunk "
            "has elapsed since the actual arrival"
        )
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "alternation", sw.trigger
        assert h.active_number() == 1

    def test_the_default_chunk_stays_well_under_half_the_ttl(self, temp_home):
        """Item 4's own precondition: `alternation_chunk_seconds` must stay
        well under `cache_ttl_seconds / 2`, so a partner alternated away
        from never goes cold before the NEXT chunk boundary alternates
        back to it (F3's "next chunk -> returns")."""
        defaults = AutoSwitchSettings()
        assert defaults.alternation_chunk_seconds * 2 < defaults.cache_ttl_seconds, (
            f"chunk={defaults.alternation_chunk_seconds} "
            f"ttl={defaults.cache_ttl_seconds} — the chunk must clear "
            "twice over before the TTL, with real margin"
        )

    # -- the alternation giveback bar ------------------------------------

    def test_alternation_refuses_a_partner_that_gives_back_the_headroom(
        self, temp_home
    ):
        """Warmth may cost headroom, never a MATERIAL regression of it:
        the arm's only admission bar was the absolute `cold_switch_cost_
        pct` floor, with no reference to `active_headroom` at all, so a
        90-headroom active departed for a 25-headroom warm partner. The
        landing rule refuses the same move when asked directly; this arm
        never reaches it (`dynamic_ordered is not None` short-circuits
        `_rank_candidates_pass`), so the bar belongs here.
        """
        h = self._harness(temp_home)
        chunk = h.engine.settings.alternation_chunk_seconds
        self._seed_last_active_at(h, {
            "1": h.clock.now - chunk,   # dwell elapsed
            "2": h.clock.now - 10.0,    # warm
        })
        outcome = h.tick_with_usage({
            "1": _usage(10.0),  # active, headroom 90
            "2": _usage(75.0),  # warm, headroom 25 -- clears the floor (20)
        })
        assert outcome is TickOutcome.NO_ACTION, (
            f"got {outcome} — a 65-point giveback is not alternation; "
            "the warm partner clears the absolute floor but hands back "
            "far more headroom than `ALTERNATION_MAX_GIVEBACK_PCT`"
        )
        assert h.active_number() == 1

    def test_alternation_still_fires_for_a_near_equal_warm_partner(
        self, temp_home
    ):
        """The anti-repeal control: an alternating move is a downgrade by
        construction, so the bar is ONE-SIDED (giveback only), never
        `_rank_candidates_pass`'s two-sided margin. Green before and
        after the fix -- if it goes red, the two-sided form was written.
        """
        h = self._harness(temp_home)
        chunk = h.engine.settings.alternation_chunk_seconds
        self._seed_last_active_at(h, {
            "1": h.clock.now - chunk,
            "2": h.clock.now - 10.0,
        })
        outcome = h.tick_with_usage({
            "1": _usage(65.0),  # active, headroom 35
            "2": _usage(72.0),  # warm, headroom 28 -- giveback 7 <= 10
        })
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome} — a near-equal warm partner is exactly the "
            "alternation #375 asked for"
        )
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "alternation", sw.trigger
        assert h.active_number() == 2

    def test_alternation_admits_a_giveback_of_exactly_the_bar(
        self, temp_home
    ):
        """The boundary is `<=`: exactly the bar is admitted."""
        # Imported HERE, never at module scope: `_base_engine_results`
        # (:2944) runs this whole module against the base commit's
        # package, and a top-level import of a symbol base does not carry
        # SKIPS all seven base-digest comparisons in silence.
        from claude_swap.autoswitch import ALTERNATION_MAX_GIVEBACK_PCT

        h = self._harness(temp_home)
        chunk = h.engine.settings.alternation_chunk_seconds
        give = ALTERNATION_MAX_GIVEBACK_PCT
        self._seed_last_active_at(h, {
            "1": h.clock.now - chunk,
            "2": h.clock.now - 10.0,
        })
        outcome = h.tick_with_usage({
            "1": _usage(60.0),          # active, headroom 40
            "2": _usage(60.0 + give),   # warm, giveback exactly `give`
        })
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome} — a giveback of exactly {give} is admitted"
        )
        assert h.active_number() == 2


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

    def test_walled_consume_first_still_holds_for_a_merely_later_reset(
        self, temp_home
    ):
        """`_walled_may_take_any_room` (c6db55c4) briefly let a WALLED
        active (headroom 1, ``about_to_wall``) below its own high departure
        threshold (99.9, still literally the `consume-first` trigger) take
        ANY real-headroom candidate regardless of reset order — an
        unauthorized change to `consume-first`'s own admission (pinned
        byte-identical to base, adr/0009): `consume-first`'s trigger is
        never produced by `dynamic`'s own classification, so the escape
        changed only `consume-first`, which this PR has no authorization to
        move. Reverted by gating that guard to `dynamic_landing`: a walled
        consume-first active still declines a candidate whose weekly reset
        is merely later, exactly as base. Prompted by
        usage-census-2026-09-07 lmd42.md §9's 171-row reset-preference
        bucket, but NOT a replay of it: that census's own rows, at their
        annotated threshold (90), already switch on this branch's floor
        without this guard (`TestUsageCensus20260907Replay`).
        """
        h = EngineHarness(temp_home, strategy="consume-first", threshold=99.9)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        outcome = h.tick_with_usage({
            "1": _usage7(99.0, 85.0, _R_SOON),    # walled: headroom 1
            "2": _usage7(0.0, 37.0, _R_LATEST),   # 63 headroom, resets LATER
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["already-consuming-soonest"]

    def test_consume_first_is_the_default_strategy(self, temp_home):
        """The owner's order: drain the account whose weekly window resets
        soonest before it resets and the quota is wasted -- opt-in no
        longer, so DEFAULT settings (no strategy given) must already switch
        to the soonest-reset candidate, not the one with most headroom."""
        h = EngineHarness(temp_home)  # strategy defaults to consume-first now
        h.seed(4, "d@example.com")  # seeded (and made live) first -> active
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.seed(5, "e@example.com")
        h.seed(6, "f@example.com")
        h.make_live("d@example.com", 4)
        now = h.clock.now
        outcome = h.tick_with_usage({
            "4": _usage7(95.0, 95.0),                        # active, over threshold
            "6": _usage7(0.0, 37.0, _iso_at(now + 532800)),   # 6d4h -- most headroom
            "3": _usage7(34.0, 44.0, _iso_at(now + 201600)),  # 2d8h
            "2": _usage7(52.0, 62.0, _iso_at(now + 309600)),  # 3d14h
            "5": _usage7(32.0, 42.0, _iso_at(now + 108000)),  # 1d6h -- soonest
            "1": _usage7(52.0, 62.0, _iso_at(now + 414000)),  # 4d19h
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 5

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

        The LIVE lock makes a second *LIVE* engine impossible, so this
        defends the layer under it: the state lock is what serializes a
        winner against any engine that reached _perform on a stale read.
        Construct the loser with the lock already released, then re-take it
        for the winner — the two are concurrent from _perform's point of
        view, which is the only view this test is about.
        """
        h = self._harness(temp_home)  # default cooldown 300s
        h.engine.stop()               # free the LIVE lock for the loser
        loser = h._make_engine()
        assert not loser.dry_run      # a demoted loser would never reach _perform
        # Release the loser's LIVE lock so the winner can take it, WITHOUT
        # setting `_stop` — a stopped engine now refuses at the freshen gate,
        # which is a different layer than the one under test here. This test is
        # about the state lock serializing two engines that both believe they
        # are running.
        loser._live_lock.release()
        loser._live_lock = None
        h.engine = h._make_engine()   # winner retakes the lock
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
        # Regression: an explicit "best" still holds below threshold even
        # when a peer resets sooner — consume-first ranking never leaks in.
        h = EngineHarness(temp_home, strategy="best")
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
        # (`_walled_may_take_any_room`'s below-threshold consume-first
        # override, c6db55c4, briefly made this tick escape immediately
        # instead — an unauthorized `consume-first` behaviour change per
        # adr/0009, reverted by gating that guard to `dynamic_landing`,
        # which `consume-first`'s literal trigger can never satisfy.)
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


class TestUsageCensus20260907Replay:
    """usage-census-2026-09-07/lmd42.md §9 names 225 "reachable wall" rows
    and annotates every one "(trig 90)" -- the fleet's own printed
    threshold. Replayed at that SAME threshold (not the higher one
    ``test_walled_consume_first_still_holds_for_a_merely_later_reset``
    needs to reach ``_walled_may_take_any_room`` at all -- a reach this
    guard no longer has for `consume-first`, gated to `dynamic_landing`),
    the active's own utilization is already >= 90 in every row, so `tick()`
    classifies `proactive`/`at-limit` -- not the literal `consume-first`
    trigger the strict reset-order filter gates on -- and the existing
    (pre-this-round) admission already switches. Mutation-checked (guard
    removed, still green): these 3 rows do not exercise THIS round's fix;
    they confirm 7d5b5d33 + 392172f1 (this branch's floor) already closes
    the exact numeric shapes the census measured, on the census's own
    stated threshold. The gap `_walled_may_take_any_room` was built for
    (`about_to_wall`, <=3pt headroom, literal `consume-first` trigger)
    needs a departure threshold configured ABOVE ~97% to keep that literal
    trigger while genuinely walled -- a shape the census's threshold=90
    fleet cannot produce, and not represented in these 225 rows; it also
    turned out to be `consume-first`'s own admission, not `dynamic`'s, so
    the guard now stays inert there. See that test for the history.

    Coverage: 3 of 225 rows (a representative sample of the 171-row
    reset-preference class: rows 39, 171, 224, its no-model,
    model-and-walled, and model-and-partial-headroom shapes). The 27-row
    model-window-as-wall and 1-row stale-usage classes are
    ``TestTheModelWindowBindsUnlessItBindsEverywhere``'s own territory
    (392172f1); a 2-account reduction of one of those rows hits
    `AllExhaustedEvent` before that retry runs (needs the original fleet's
    account count), so it is not replayed here.
    """

    @staticmethod
    def _replay(temp_home, active_windows, peer_windows, *, threshold=90.0, model=None):
        kwargs = {"strategy": "consume-first", "threshold": threshold}
        if model:
            kwargs["model"] = model
        h = EngineHarness(temp_home, **kwargs)
        h.seed(1, "active@example.com")
        h.seed(2, "peer@example.com")
        h.make_live("active@example.com", 1)
        active = {"five_hour": {"pct": active_windows[0]},
                  "seven_day": {"pct": active_windows[1], "resets_at": _R_SOON}}
        peer = {"five_hour": {"pct": peer_windows[0]},
                "seven_day": {"pct": peer_windows[1], "resets_at": _R_LATEST}}
        if len(active_windows) > 2:
            active["scoped"] = [{"name": "Fable", "pct": active_windows[2],
                                  "resets_at": _R_SOON}]
            peer["scoped"] = [{"name": "Fable", "pct": peer_windows[2],
                                "resets_at": _R_LATEST}]
        outcome = h.tick_with_usage({"1": active, "2": peer})
        return outcome, h

    def test_row_39_reset_preference(self, temp_home):
        # lmd42.md §9 row 39: active #4 91% (0/64/91), best candidate #6
        # (1/55/77). No candidate resets sooner -> old code declined.
        outcome, h = self._replay(temp_home, (0.0, 91.0), (1.0, 77.0))
        assert outcome is TickOutcome.SWITCHED, outcome
        assert h.active_number() == 2

    def test_row_171_reset_preference(self, temp_home):
        # lmd42.md §9 row 171: active #4 100% (94/85/100), best candidate
        # #2 (0/37/51).
        outcome, h = self._replay(
            temp_home, (94.0, 85.0, 100.0), (0.0, 37.0, 51.0), model="Fable"
        )
        assert outcome is TickOutcome.SWITCHED, outcome
        assert h.active_number() == 2

    def test_row_224_reset_preference(self, temp_home):
        # lmd42.md §9 row 224: active #4 100% (8/86/100), best candidate
        # #2 (15/60/82).
        outcome, h = self._replay(
            temp_home, (8.0, 86.0, 100.0), (15.0, 60.0, 82.0), model="Fable"
        )
        assert outcome is TickOutcome.SWITCHED, outcome
        assert h.active_number() == 2


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


class TestPhase2RefetchKeepsTheDynamicModelBasis:
    """`dynamic`'s model-basis widening (the re-pick that turns a pinned
    model's own window into "not a blackout" for the ACTIVE, above) must
    still be the basis `_perform` records into `state["leftHeadroom"]" on
    a real switch. Two accounts, `--model all`: both read model-gated
    headroom 0 (Fable pinned at 100%) but real 5h/7d room, so the widened
    basis is the only thing that tells the engine it is not genuinely at
    the wall.

    #375 dropped the bare below-threshold trigger (`trigger =
    settings.strategy`) `dynamic` used to classify with, so a live tick
    never produces the literal `"dynamic"` any more and the consume-first
    two-phase commit's refetch (gated on `trigger in
    CONSUME_FIRST_STRATEGIES`) is consume-first-only in practice now.
    These fixtures reach `about_to_wall` (headroom <= 3) instead, so the
    trigger is `proactive` and the ordinary hysteresis-margin admission
    (unchanged) is what a real candidate must clear — the widened basis
    still has to be what `_perform` records, refetch or not.
    """

    def test_leftHeadroom_is_recorded_on_the_widened_basis_not_the_model_gated_one(
        self, temp_home
    ):
        h = EngineHarness(
            temp_home, model="all", threshold=95.0, hysteresis_pct=20.0,
            strategy="dynamic",
        )
        h.seed(1, "acct1@example.invalid")
        h.seed(2, "acct2@example.invalid")
        h.make_live("acct1@example.invalid", 1)

        def acct(five_h, seven_d, fable, hours_out):
            return {
                "five_hour": {"pct": five_h},
                "seven_day": {
                    "pct": seven_d,
                    "resets_at": _iso_at(h.clock.now + hours_out * 3600),
                },
                "scoped": [{"name": "Fable", "pct": fable}],
            }

        out = h.tick_with_usage({
            "1": acct(5, 97, 100, 14),   # model-gated headroom 0, unmodeled 3 (about_to_wall)
            "2": acct(10, 10, 100, 8),   # model-gated headroom 0, unmodeled 90 -- real room
        })
        assert out is TickOutcome.SWITCHED
        assert h.active_number() == 2
        state = h.engine._read_state()
        assert state.get("leftHeadroom") == 3.0, (
            f"leftHeadroom={state.get('leftHeadroom')!r} — must record the "
            "SAME widened basis (3.0, account 1's unmodeled 5h/7d "
            "headroom) the trigger was classified on, not the narrower "
            "model-gated 0.0"
        )

    def test_a_wrong_leftHeadroom_of_zero_does_not_cause_a_two_tick_ping_pong_back(
        self, temp_home
    ):
        """The failure mode a dropped widening produces: `leftHeadroom: 0.0`
        makes `_left_account_recovered`'s headroom leg release on account
        1's very next model-gated headroom (`h >= min(0+3, 100)`, i.e. any
        h >= 3), for essentially free -- and the engine switches straight
        back, the two-tick ping-pong the landing rule exists to stop. With
        the widened basis (3.0) that same leg needs `h >= 6`, which
        account 1's model-gated 3.0 does not clear."""
        h = EngineHarness(
            temp_home, model="all", threshold=95.0, hysteresis_pct=20.0,
            strategy="dynamic",
        )
        h.seed(1, "acct1@example.invalid")
        h.seed(2, "acct2@example.invalid")
        h.make_live("acct1@example.invalid", 1)

        def acct(five_h, seven_d, fable, hours_out):
            return {
                "five_hour": {"pct": five_h},
                "seven_day": {
                    "pct": seven_d,
                    "resets_at": _iso_at(h.clock.now + hours_out * 3600),
                },
                "scoped": [{"name": "Fable", "pct": fable}],
            }

        out1 = h.tick_with_usage({
            "1": acct(5, 97, 100, 14),
            "2": acct(10, 10, 100, 8),
        })
        assert out1 is TickOutcome.SWITCHED
        assert h.active_number() == 2

        h.clock.advance(301.0)  # past cooldown_seconds (default 300)
        out2 = h.tick_with_usage({
            "1": acct(20, 5, 97, 5),    # model-gated headroom 3.0
            "2": acct(92, 20, 90, 30),  # holds real headroom, well over 3
        })
        assert out2 is TickOutcome.NO_ACTION, (
            f"got {out2}, active now {h.active_number()!r} — account 1's "
            "3.0-point model-gated headroom must not read as 'recovered' "
            "against a leftHeadroom baseline that was itself wrong"
        )
        assert h.active_number() == 2


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

    def test_at_limit_ranks_on_the_window_that_actually_blocked(self, harness):
        """Escaping a 5h limit must rank candidates by their 5h room.

        `account_headroom` is `100 - max(5h, 7d, scoped...)` — the BINDING
        window. That is the right number for "is this account usable at all"
        and the wrong one for "which account best escapes the window that just
        blocked me". At-limit skips every proactive gate, so the only thing
        left deciding the target is the sort key, and that key was the max.

        Active is blocked on 5h. Candidate 2 has a FULL 5h window and a burnt
        weekly; candidate 3 has most of its 5h spent and a fresh weekly. Both
        are usable, so neither is filtered — only the ORDER is at issue. For
        the next five hours candidate 2 is worth 100 points of the axis that
        blocked us and candidate 3 is worth 15, but max() scores them 10 and
        15 and the old key took the one with almost no room where it counts.
        """
        outcome = harness.tick_with_usage({
            "1": {"five_hour": {"pct": 100.0}, "seven_day": {"pct": 20.0}},
            "2": {"five_hour": {"pct": 0.0},   "seven_day": {"pct": 90.0}},
            "3": {"five_hour": {"pct": 85.0},  "seven_day": {"pct": 10.0}},
        })
        assert outcome is TickOutcome.SWITCHED
        sw = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "at-limit"
        assert harness.active_number() == 2, (
            "a 5h limit fired, so the escape must go to the account with the "
            "most 5h room (2: 100 free) rather than the best worst-case "
            "window (3: max(85,10)=85 -> headroom 15 beats 2's 10)"
        )

    def test_the_escape_axis_is_not_cancelled_by_the_consume_first_strategy(
        self, temp_home
    ):
        """Same case, `strategy=consume-first` — the answer must not change.

        `consume_first` is the configured STRATEGY, not the trigger, and its
        arm sat ahead of the escape arm. So an at-limit escape for a
        consume-first user ranked on the soonest weekly reset and never
        reached the window that had actually blocked it: the feature applied
        to half the users. The same shape the recovery-hysteresis fix
        already closed one branch above — filtering on one axis while
        sorting on another.

        The consume-first PREFERENCE is proactive (which account to burn
        next); at-limit is not a preference, it is a session that is stopped.
        """
        h = EngineHarness(temp_home, strategy="consume-first")
        h.seed(1, "a@example.com"); h.seed(2, "b@example.com")
        h.seed(3, "c@example.com"); h.make_live("a@example.com", 1)
        outcome = h.tick_with_usage({
            "1": {"five_hour": {"pct": 100.0}, "seven_day": {"pct": 20.0}},
            "2": {"five_hour": {"pct": 0.0},   "seven_day": {"pct": 90.0}},
            "3": {"five_hour": {"pct": 85.0},  "seven_day": {"pct": 10.0}},
        })
        assert outcome is TickOutcome.SWITCHED
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "at-limit"
        assert h.active_number() == 2, (
            "consume-first ranked the escape on the weekly reset, so a 5h "
            "limit sent the user to the account with 15 points of 5h room "
            "instead of the one with 100"
        )

    def test_control_a_7d_limit_still_ranks_on_the_weekly_axis(self, harness):
        """CONTROL for the case above, and it is what makes it a fix rather
        than a preference. Flip which window blocks the active account and the
        answer must flip with it — otherwise the change is "always prefer 5h",
        which would be a different bug wearing this one's clothes."""
        outcome = harness.tick_with_usage({
            "1": {"five_hour": {"pct": 20.0},  "seven_day": {"pct": 100.0}},
            "2": {"five_hour": {"pct": 0.0},   "seven_day": {"pct": 90.0}},
            "3": {"five_hour": {"pct": 85.0},  "seven_day": {"pct": 10.0}},
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3, (
            "a 7d limit fired, so the escape must go to the account with the "
            "most WEEKLY room (3: 90 free) — the mirror of the case above"
        )


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
        unbarred, _, _, _ = harness.engine._rank_candidates(no_return=None, **args)
        barred, _, _, _ = harness.engine._rank_candidates(no_return="1", **args)

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
        """`_binding_recovery_ts` returns `inf` in THREE states, and NONE of
        them means "never": no relevant window at all, no blocking window
        naming a parseable `resets_at`, and every blocking reset already
        elapsed. All three mean "we do not know". The old
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
        """`math.isfinite(active_recovery_ts)` reads ALL THREE `inf` states
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
        # Past `reset_at` (a real re-read regression must show), but within
        # `SERVE_TTL_S` (180) of candidate #2's `fetched_at` (== ranking_now)
        # — one value that exposes a stray re-read wherever in the call
        # order it lands, without also tripping the UNRELATED stale-usage
        # admission gate (which reads `self.clock()` too, at a position
        # that shifts by one call between the two code paths below).
        late_reread = ranking_now + 150.0

        # Measured call order: pre-tick usage collection, ranking's own
        # `decided_now`, THEN — only on the pre-#321 code this guards
        # against — `left_snapshot`'s own re-read, then the stale-usage
        # admission gate's freshness read, freshen's expiry check, and
        # `_perform`'s `lastSwitchAt`. `late_reread` covers every position
        # from the admission gate onward so the same list works whether or
        # not the extra re-read call is present, with spares left over.
        clock_values = iter([
            ranking_now, ranking_now, late_reread, late_reread,
            late_reread, late_reread, late_reread,
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

    @pytest.mark.parametrize("case,pcts", [
        # The 18:50 shape: nowhere better, so staying is right. The spent-band
        # rule decides where to sit, not an early escape.
        ("at the brink with only spent peers", (99, 100, 100)),
        # A comfortable account is untouched: the hysteresis margin applies.
        ("below the brink, ordinary rules", (50, 45, 40)),
    ])
    def test_it_holds(self, harness, case, pcts):
        """Both halves of "no early escape": the two ends of the range hold for
        different reasons and neither needs a trigger of its own."""
        active, peer2, peer3 = pcts
        outcome = harness.tick_with_usage({
            "1": _usage(active, self._at(harness, 109 * 3600)),
            "2": _usage(peer2, self._at(harness, 80 * 3600)),
            "3": _usage(peer3, self._at(harness, 50 * 3600)),
        })
        assert outcome is not TickOutcome.SWITCHED, case


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

class TestLiveLock:
    """Only one LIVE engine per machine.

    The hazard this closes: `_perform`'s state lock serializes the *write*,
    but the at-limit and failover triggers skip the cooldown check entirely
    (see `_perform`: only proactive/consume-first consult `_in_cooldown`), so
    two LIVE engines both decide to switch and the second undoes the first's
    choice. Measured in the field with two TUIs on one machine.
    """

    def test_second_live_engine_demotes_to_dry_run(self, harness):
        second = harness._make_engine()
        assert second.dry_run is True
        assert second.demoted_from_live is True
        # The winner is untouched.
        assert harness.engine.dry_run is False

    def test_a_demoted_engine_does_not_switch(self, harness):
        second = harness._make_engine()
        events: list = []
        second.on_event = events.append
        with patch.object(
            harness.switcher,
            "usage_entries_by_account",
            return_value={
                num: _entry_for(value, harness.clock.now)
                for num, value in {"1": _usage(95), "2": _usage(5)}.items()
            },
        ):
            outcome = second.tick()
        # NO_ACTION, not SWITCHED. `cli.py` documents 0 as "switched to
        # another account" and this process switched nothing — the engine
        # holding the LIVE lock is the one that will. Reporting 0 to a cron
        # wrapper is a lie about the active account. The event still carries
        # dry_run so the decision itself stays visible.
        assert outcome is TickOutcome.NO_ACTION
        assert harness.active_number() == 1        # changed nothing
        assert any(e.dry_run for e in events if isinstance(e, SwitchEvent))

    def test_tick_keeps_its_never_raises_promise_when_the_lock_dir_is_unwritable(
        self, harness
    ):
        """`_retry_live_promotion` runs BEFORE tick()'s try, on purpose.

        The preamble's ordering is load-bearing and documented, so the guard
        belongs at the raising call rather than in a restructured tick. What
        must hold either way is the docstring: tick() returns an outcome.
        `cli.py` does `sys.exit(engine.tick().value)` with a documented
        0/1/2/3 contract, and an escaping OSError replaces that with a
        traceback and interpreter exit 1.
        """
        from claude_swap import autoswitch as autoswitch_mod

        engine = harness._make_engine()          # demoted: the fixture holds LIVE
        assert engine.demoted_from_live is True, (
            "premise: only a demoted engine retries the promotion"
        )

        def unwritable(self, *a, **kw):
            raise OSError(30, "Read-only file system")

        with patch.object(autoswitch_mod.FileLock, "acquire", unwritable):
            outcome = engine.tick()

        assert isinstance(outcome, TickOutcome)

    def test_constructing_a_live_engine_on_an_unwritable_dir_does_not_raise(
        self, harness
    ):
        """The same call, one frame earlier, with no guard on it.

        `_retry_live_promotion` catches `OSError` from `acquire()` because the
        acquire CREATES the lock's directory and file, so an unwritable
        `backup_dir` raises. `__init__` makes the identical call and did not,
        and it is on the ordinary CLI path: `cswap auto` builds the engine
        before `_auto_command`'s `except ClaudeSwitchError/KeyboardInterrupt`
        can mean anything, so a read-only backup dir replaced the documented
        0/1/2/3 exit with a traceback.

        Demoting is the answer the loser already gets. A machine that cannot
        take the lock cannot be the LIVE engine, whoever holds it and for
        whatever reason.
        """
        from claude_swap import autoswitch as autoswitch_mod

        def unwritable(self, *a, **kw):
            raise OSError(30, "Read-only file system")

        with patch.object(autoswitch_mod.FileLock, "acquire", unwritable):
            engine = harness._make_engine(dry_run=False)

        assert engine.dry_run is True, "it must not act without the lock"
        assert engine.demoted_from_live is True, (
            "the demotion is what the TUI renders, so it has to be set"
        )

    def test_a_lock_that_cannot_be_CREATED_is_not_reported_as_contention(
        self, harness
    ):
        """Demoting silently is worse than the raise it replaced.

        The round-5 fix swallowed `OSError` from `acquire()` so a read-only
        backup dir could not replace `cswap auto`'s documented 0/1/2/3 exit
        with a traceback. It took the CONTENTION branch to do it, so a
        writable dir with an unwritable `.auto-live.lock` — a root run, a
        tight umask — now says another engine holds the lock. There is no
        other engine, nothing is logged, `_retry_live_promotion` re-raises the
        same errno every tick so it never recovers, and every tick decides to
        switch and does not. Before the fix this raised loudly.

        The demotion is still right; the SENTENCE has to be about what
        happened.
        """
        from claude_swap import autoswitch as autoswitch_mod

        def unwritable(self, *a, **kw):
            raise OSError(13, "Permission denied")

        events: list = []
        with patch.object(autoswitch_mod.FileLock, "acquire", unwritable):
            engine = harness._make_engine(dry_run=False)
            engine.on_event = events.append
            with patch.object(
                harness.switcher, "usage_entries_by_account",
                return_value={
                    num: _entry_for(value, harness.clock.now)
                    for num, value in {"1": _usage(95), "2": _usage(5)}.items()
                },
            ):
                engine.tick()

        assert engine.demoted_from_live is True, "premise: it did not demote"
        said = [e.message for e in events
                if isinstance(e, ConfigWarningEvent)]
        assert said, "the demotion was silent — nothing said why it stopped"
        assert not any("already running" in m for m in said), (
            f"a lock that could not be created was reported as contention: {said}"
        )
        assert any("Permission denied" in m or "denied" in m.lower()
                   for m in said), (
            f"the cause the operator has to fix is not in the message: {said}"
        )

    def test_a_user_requested_dry_run_still_reports_switched(self, harness):
        """The other arm, which the demoted assertion must not take with it.

        `--dry-run` is a question: "what would you do?". SWITCHED answers it,
        and that is the contract this engine had before demotion started
        borrowing the same flag.
        """
        engine = harness.engine
        engine.dry_run = True
        assert engine.demoted_from_live is False, (
            "premise: this engine is dry-run BY REQUEST, not by demotion"
        )
        events: list = []
        engine.on_event = events.append
        with patch.object(
            harness.switcher,
            "usage_entries_by_account",
            return_value={
                num: _entry_for(value, harness.clock.now)
                for num, value in {"1": _usage(95), "2": _usage(5)}.items()
            },
        ):
            outcome = engine.tick()
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 1
        assert any(e.dry_run for e in events if isinstance(e, SwitchEvent))

    def test_stop_is_idempotent_and_reentrant(self, harness):
        """`cli.py` installs `stop()` as the SIGTERM handler on the MAIN thread.

        Python delivers signals on the main thread, interrupting whatever
        frame is running — including `_perform`, which owns the in-flight
        flag. So `stop()` can run ON TOP of the frame that would clear it,
        and a second SIGTERM (an impatient operator, systemd's retry) runs a
        second handler nested on the same thread.

        Measured before the guard: both frames reached
        `self._live_lock.release()` and the inner one raised

            AttributeError: 'NoneType' object has no attribute 'release'

        which propagated into `_perform` INSIDE `with self._state_lock():`,
        past `atomic_write_json` — the account switched but `lastSwitchAt` was
        never written, so the next engine saw no cooldown. Eight concurrent
        `stop()` calls produced `ValueError: I/O operation on closed file`
        from `FileLock.release()` double-closing.
        """
        import threading

        engine = harness.engine
        errors: list = []

        def call_stop():
            try:
                engine.stop()
            except Exception as e:  # noqa: BLE001 - the point of the test
                errors.append(f"{type(e).__name__}: {e}")

        # NESTED, the signal shape: stop() runs on top of a stop() that is
        # inside its own wait. A plain barrier cannot model this — the second
        # frame must start while the first is blocked, on the SAME thread.
        engine._tick_in_flight.clear()
        depth = {"max": 0, "cur": 0}
        real_wait = engine._tick_in_flight.wait

        def reentrant_wait(timeout=None):
            depth["cur"] += 1
            depth["max"] = max(depth["max"], depth["cur"])
            try:
                if depth["cur"] == 1:
                    # The second SIGTERM, delivered on the same thread while
                    # the first handler is inside its wait. It must not touch
                    # the lock the outer frame is about to release.
                    call_stop()
                return True
            finally:
                depth["cur"] -= 1

        engine._tick_in_flight.wait = reentrant_wait
        try:
            call_stop()
        finally:
            engine._tick_in_flight.wait = real_wait
            engine._tick_in_flight.set()

        assert depth["max"] >= 1, "premise: the wait was reached"
        assert errors == [], f"a nested stop() raised {errors}"

        threads = [threading.Thread(target=call_stop) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        assert errors == [], f"concurrent stop() raised {errors}"
        engine.stop()          # and again, serially
        assert engine._live_lock is None

    def test_the_switch_flag_is_armed_before_the_stop_gate(self, harness):
        """The deferral had a two-statement hole at its own entrance.

        `_perform` tested `_stop` and THEN armed `_switch_in_flight`. A signal
        handler runs inside the frame it interrupts, so a SIGTERM between them
        found `own_tick=True, _switch_in_flight=False`, and `stop()` took the
        immediate-release path: measured, the switch ran to completion with
        LIVE already released and claimable by a successor.

        Asserts the ORDER directly. Driving a signal into that window needs
        `_stop.is_set` stubbed, and `stop()` calls it too — measured, the stub
        changes `stop()`'s own behaviour, so the test then reports a release
        on the FIXED code as well. A test whose instrument perturbs the thing
        it measures answers a different question; the order is the property,
        and it is observable without touching the runtime.
        """
        import inspect

        src = inspect.getsource(harness.engine._perform)
        arm = src.index("self._switch_in_flight = True")
        # THE GATE THAT GUARDS THE SWITCH, not any gate after the arm. Asking
        # "is there a gate below the arm" is `arm < max(gates)`, which a new
        # checkpoint anywhere below `switch_to` satisfies while the SIGTERM
        # window this exists for is fully reopened. The subject is the last
        # gate BEFORE the switch; the byte window this replaced picked its
        # gate by distance and raised `ValueError` once the anchor moved.
        gate = src.rfind("if self._stop.is_set():", 0, src.index("self.switcher.switch_to("))
        assert gate != -1, "premise: no `_stop` gate precedes the switch call"
        assert arm < gate, (
            "`_switch_in_flight` is armed after the `_stop` gate that guards "
            "the switch; a signal between them takes stop()'s "
            "immediate-release path"
        )

    def test_a_stopped_engine_does_not_fetch_usage(self, harness):
        """`stop()` returns while a worker is parked in an emit — by design.

        The exemption keeps the TUI from deadlocking, but the worker then
        WAKES and keeps going. Between `_tick_inner`'s entry gate and the
        freshen loop sits the usage collection, which POSTs one-time refresh
        grants. Measured before the fix: `usage fetches AFTER LIVE was
        released: [['2']]` — a stopped engine consuming grants for a
        successor that already owns the lock.

        Asserts on the FETCH, not on the outcome: a tick that returns
        NO_ACTION after fetching has already spent the grant.
        """
        engine = harness.engine
        fetched: list = []

        # Stopped DURING the tick, not before it. `_tick_inner`'s entry gate
        # already covers an engine stopped beforehand — measured, driving it
        # that way never reaches the collection at all, so the test passed
        # with the checkpoint removed. The real shape is a `stop()` landing
        # after the tick began, which is every TUI toggle and every SIGTERM.
        # Stopped on the FIRST no-network read, which is `_collect_scheduled
        # _usage`'s own `fetch=set()` probe — the last point before the three
        # network fetches. That is where a `stop()` released by the emit
        # exemption actually lands relative to them; hooking a later emit put
        # the stop AFTER the collection and the test passed with every guard
        # removed.
        calls = {"n": 0}
        real_entries = harness.switcher.usage_entries_by_account

        def record(*a, **kw):
            calls["n"] += 1
            if kw.get("fetch") or (a and a[0]):
                import traceback
                fr = [f for f in traceback.extract_stack()
                      if f.filename.endswith("/claude_swap/autoswitch.py")]
                fetched.append(fr[-1].lineno if fr else None)
            if calls["n"] == 1:
                engine._stop.set()      # the stop lands here
            return real_entries(*a, **kw)

        with patch.object(harness.switcher, "usage_entries_by_account", record):
            engine.tick()

        assert fetched == [], (
            f"a stopped engine ran {len(fetched)} usage fetch(es); the grants "
            "belong to whoever holds LIVE now"
        )

    def test_a_stop_during_the_promotion_does_not_strand_the_lock(
        self, harness
    ):
        """`acquire()` and the assignment are two statements.

        `stop()` between them reads `_live_lock is None`, returns, and the
        promotion then hands a real cross-process flock to an engine that will
        never tick again. Nothing reclaims it — `_release_live` runs only from
        `_perform`'s finally — so no engine on the machine can go LIVE until
        the process exits. Measured before this:
        `stopped=True holds_lock=True successor_demoted=True`.

        Asserts a SUCCESSOR can take LIVE, not that `_live_lock` is None: the
        attribute is bookkeeping, the flock is the resource.
        """
        from claude_swap.locking import FileLock

        demoted = harness._make_engine(dry_run=False)
        assert demoted.demoted_from_live, "premise: the harness holds LIVE"
        harness.engine.stop()                       # the holder exits

        real_acquire = FileLock.acquire

        def acquire_then_stop(self, *a, **kw):
            ok = real_acquire(self, *a, **kw)
            if ok:
                demoted.stop()                      # lands in the window
            return ok

        FileLock.acquire = acquire_then_stop
        try:
            demoted._retry_live_promotion()
        finally:
            FileLock.acquire = real_acquire

        successor = harness._make_engine(dry_run=False)
        try:
            assert not successor.demoted_from_live, (
                "a stopped engine still holds the LIVE flock; nothing on this "
                "machine can go LIVE until the process exits"
            )
        finally:
            successor.stop()

    def test_a_lock_error_that_changes_is_said_once_more(self, harness):
        """A cause that CHANGES has to be announced again.

        The first attempt loses on contention, so the operator is told another
        LIVE engine is running. The holder then exits while `backup_dir` goes
        read-only, and every attempt after that raises EROFS -- without the
        re-arm the operator hunts a process that no longer exists for the life
        of the run. Deleting the four lines that record it left the whole
        suite green.
        """
        import errno as _errno

        from claude_swap.locking import FileLock

        demoted = harness._make_engine(dry_run=False)
        try:
            assert demoted.demoted_from_live, "premise: the harness holds LIVE"
            real_acquire = FileLock.acquire
            box = [OSError(_errno.EACCES, "first")]

            def raising(self, *a, **kw):
                raise box[0]

            FileLock.acquire = raising
            try:
                demoted._demotion_announced = True
                demoted._retry_live_promotion()
                assert demoted._demotion_announced is False, (
                    "the first error was not recorded, so nothing re-announces"
                )

                demoted._demotion_announced = True
                demoted._retry_live_promotion()          # the SAME cause
                assert demoted._demotion_announced is True, (
                    "an unchanged cause was re-announced, so the operator gets "
                    "the same sentence every tick"
                )

                box[0] = OSError(_errno.EROFS, "second")  # the cause CHANGES
                demoted._retry_live_promotion()
                assert demoted._demotion_announced is False, (
                    "a changed cause was not re-armed, so whatever stopped the "
                    "FIRST attempt is reported for the life of the run"
                )
            finally:
                FileLock.acquire = real_acquire
        finally:
            demoted.stop()

    def test_a_promotion_before_any_announcement_stays_silent(self, harness):
        """An errno recorded while nothing has been announced yet.

        That is the only state that discriminates the clearing: the cause is
        armed, the promotion then succeeds, and without the flag the
        reordered announce emits a stale cause AFTER "now LIVE".
        """
        import errno as _errno

        from claude_swap.locking import FileLock

        demoted = harness._make_engine(dry_run=False)
        try:
            assert demoted.demoted_from_live, "premise: the harness holds LIVE"
            real_acquire = FileLock.acquire

            def refuse(self, *a, **kw):
                raise OSError(_errno.EROFS, "read-only file system")

            FileLock.acquire = refuse
            try:
                demoted._retry_live_promotion()      # records, announces NOTHING
            finally:
                FileLock.acquire = real_acquire
            assert demoted._live_lock_error is not None, (
                "premise: the errno was never recorded"
            )
            assert demoted._demotion_announced is False, (
                "premise: something announced already, so this cannot "
                "discriminate the clearing"
            )

            harness.engine.stop()                    # the holder exits
            before = len(harness.events)
            demoted.tick()                           # this one promotes

            said = [getattr(e, "message", "") for e in harness.events[before:]]
            assert any("now LIVE" in m for m in said), (
                f"premise: the engine never promoted: {said}"
            )
                # COUNTED, NOT MATCHED. Keying on prose ties this to two
            # sentences forever, and a rewording of either one disarms it
            # silently. A promoting tick owes exactly one event, the
            # promotion's own.
            warned = [e for e in harness.events[before:]
                      if isinstance(e, ConfigWarningEvent)]
            assert len(warned) == 1, (
                "the promotion did not clear the cause it resolved, so a "
                "stale fault is announced after the engine went LIVE: "
                f"{[e.message for e in warned]}"
            )
        finally:
            demoted.stop()

    def test_contention_clears_an_errno_it_no_longer_explains(self, harness):
        """A cause that goes ERRNO -> CONTENTION must stop being reported.

        The errno arm re-arms when the cause changes; the contention arm was a
        bare `return`, so a run that started on an unwritable `backup_dir` and
        later lost only to another engine kept naming the filesystem fault for
        the life of the run -- the same harm the errno arm exists to prevent,
        in the direction it did not cover.
        """
        import errno as _errno

        from claude_swap.locking import FileLock

        demoted = harness._make_engine(dry_run=False)
        try:
            assert demoted.demoted_from_live, "premise: the harness holds LIVE"
            real_acquire = FileLock.acquire
            mode = {"raise": True}

            def flaky(self, *a, **kw):
                if mode["raise"]:
                    raise OSError(_errno.EROFS, "read-only file system")
                return False        # plain contention: somebody holds it

            FileLock.acquire = flaky
            try:
                demoted._retry_live_promotion()
                assert demoted._live_lock_error is not None, (
                    "premise: the errno was never recorded"
                )
                demoted._demotion_announced = True

                mode["raise"] = False          # the mount is fixed; a peer wins
                demoted._retry_live_promotion()
                assert demoted._live_lock_error is None, (
                    "contention left a filesystem errno recorded, so the "
                    "operator keeps being told to fix a mount that is fine"
                )
                assert demoted._demotion_announced is False, (
                    "the cause changed and nothing re-armed the announcement"
                )
            finally:
                FileLock.acquire = real_acquire
        finally:
            demoted.stop()

    def test_a_stop_one_statement_later_does_not_strand_the_lock(
        self, harness
    ):
        """The sibling test fires `stop()` from inside `FileLock.acquire`.

        That lands BEFORE the re-check, which is exactly the case the
        re-check catches. One statement later — after the re-check passed and
        before `_live_lock = lock` — reproduces the original symptom verbatim:
        `stop()` reads `_live_lock is None`, takes its idempotent early
        return, and the assignment then hands a live cross-process flock to an
        engine that will never tick again. Nothing reclaims it, so no engine
        on the machine can go LIVE until the process exits.

        Same window, second symptom: `dry_run = False` is published with no
        stop re-check after it either, and `autoview._update_badge` renders
        " LIVE " from exactly `not engine.dry_run` — so a dead engine reads
        LIVE. `test_a_stopped_engine_is_not_badged_live` covers the plain
        `stop()` path; this is the other way in.

        A check-then-act pair cannot be closed by adding a third check, so
        this drives the stop through `_stop_lock` — the one lock `stop()`
        itself takes. Serializing against that lock is what "indivisible with
        respect to `stop()`" means here; a fix that publishes outside it
        leaves the window open no matter how many checks precede it.
        """
        demoted = harness._make_engine(dry_run=False)
        assert demoted.demoted_from_live, "premise: the harness holds LIVE"
        harness.engine.stop()                       # the holder exits

        real_lock = demoted._stop_lock
        fired: list[bool] = []

        class _StopInsideTheWindow:
            """`stop()` lands after the re-check, before the publish."""

            def __enter__(self):
                real_lock.__enter__()
                # Once: the `stop()` below re-enters this same RLock.
                if not fired:
                    fired.append(True)
                    demoted.stop()
                return self

            def __exit__(self, *exc):
                return real_lock.__exit__(*exc)

        demoted._stop_lock = _StopInsideTheWindow()
        try:
            demoted._retry_live_promotion()
        finally:
            demoted._stop_lock = real_lock

        assert fired, (
            "premise: the acquire->publish transition must run under "
            "`_stop_lock`, the lock `stop()` also takes — outside it the "
            "window stays open and no number of re-checks closes it"
        )
        assert demoted.dry_run, (
            "a stopped engine reports dry_run=False after the promotion, so "
            "the badge reads LIVE for an engine that will never tick"
        )
        successor = harness._make_engine(dry_run=False)
        try:
            assert not successor.demoted_from_live, (
                "a stopped engine still holds the LIVE flock; nothing on this "
                "machine can go LIVE until the process exits"
            )
        finally:
            successor.stop()

    def test_a_raising_consumer_does_not_escape_the_tick(self, harness):
        """`tick()` documents "Never raises" and its try covers only
        `_tick_inner`.

        So an emit from `_announce_demotion` / `_retry_live_promotion` (before
        the try) or from the except handlers (outside it) escaped. Measured
        through the real CLI: `cswap auto --once --json | head -1` closed the
        pipe and the documented 0/1/2/3 exit contract became a BrokenPipeError
        traceback, losing the tick's outcome.

        Drives BOTH shapes — the ordinary path and a pending demotion
        announcement — because fixing only the pre-try emits leaves the
        handlers open.
        """
        engine = harness.engine
        engine.on_event = lambda ev: (_ for _ in ()).throw(
            BrokenPipeError("consumer went away")
        )
        engine.tick()                     # must not raise

        second = harness._make_engine(dry_run=False)
        second.demoted_from_live = True
        second._demotion_announced = False
        second.on_event = lambda ev: (_ for _ in ()).throw(
            BrokenPipeError("consumer went away")
        )
        try:
            second.tick()                 # the pre-try emit path
        finally:
            second.stop()

    def test_a_stopped_engine_is_not_badged_live(self, harness):
        """`autoview` renders the badge from `not engine.dry_run`.

        Leaving it False after the release made a dead engine read " LIVE ".
        Masked in the normal flow because `_restart_engine` replaces `_engine`
        at once — but `_start_engine` can raise after this `stop()`, and the
        screen then points at the stopped one.
        """
        engine = harness.engine
        assert not engine.dry_run, "premise: this engine is LIVE"
        engine.stop()
        assert engine.dry_run, (
            "a stopped engine still reports dry_run=False, so the badge reads "
            "LIVE for an engine that will never tick"
        )

    def test_a_stop_mid_collection_is_not_an_unhealthy_tick(self, harness):
        """The stop-returns were indistinguishable from a failed fetch.

        Both guards returned an empty triple, so `headroom.get(current)` came
        back None and the tick charged `_unhealthy_ticks` — measured 0 -> 1 —
        while emitting nothing, so a `--once` run answered NO_ACTION with no
        reason line. Every other `_stop` checkpoint emits `engine-stopped`.
        """
        engine = harness.engine
        before = engine._unhealthy_ticks
        # Stopped on the collector's no-network probe — the last point before
        # its two network fetches, which is where the guards live. Stopping
        # earlier hits `_tick_inner`'s entry gate instead, and that one has
        # always emitted; the test would then pass without reaching the
        # guards at all.
        calls = {"n": 0}
        real = harness.switcher.usage_entries_by_account

        def stop_at_the_probe(*a, **kw):
            calls["n"] += 1
            out = real(*a, **kw)
            if calls["n"] == 1:
                engine._stop.set()
            return out

        harness.events.clear()
        with patch.object(
            harness.switcher, "usage_entries_by_account", stop_at_the_probe
        ):
            engine.tick()

        assert engine._unhealthy_ticks == before, (
            f"unhealthy_ticks {before} -> {engine._unhealthy_ticks}: a stop is "
            "not a fetch failure"
        )
        assert any(
            getattr(e, "reason", None) == "engine-stopped"
            for e in harness.events
        ), (
            f"events {[getattr(e, 'reason', type(e).__name__) for e in harness.events]}"
            " — the tick abandoned itself silently"
        )

    def test_a_demoted_engine_takes_live_once_the_holder_releases_it(
        self, harness
    ):
        """The demotion is decided in `__init__` and never revisited.

        A second TUI demotes to dry-run because the first holds the LIVE lock.
        That is right at the time. But when the first exits, the lock is free
        and nothing re-checks: `demoted_from_live` stays True, `_live_lock`
        stays None, and the dashboard reads DRY-RUN forever with no indication
        it will never change. The user's intent was LIVE — the demotion was a
        contention answer, not a preference.

        Asserts on the ENGINE's own state after a tick, not on the badge: the
        badge renders `dry_run` correctly either way, which is what made this
        invisible.
        """
        from claude_swap.locking import FileLock

        # The harness engine already holds LIVE — it IS the first TUI.
        assert harness.engine._live_lock is not None, "premise: harness is LIVE"

        second = harness._make_engine(dry_run=False)
        assert second.demoted_from_live and second.dry_run, (
            "premise: the second engine demoted"
        )

        harness.engine.stop()    # the first TUI exits, releasing LIVE

        with patch.object(
            harness.switcher, "usage_entries_by_account", return_value={}
        ):
            second.tick()

        assert not second.dry_run, (
            "the holder is gone and LIVE is free, but the engine is still in "
            "dry-run — nothing re-checks, so it can never come back"
        )
        second.stop()

    def test_a_stopped_demoted_engine_makes_no_flock_attempt(self, harness):
        """`:841` (`if not self.demoted_from_live or self._stop.is_set():
        return`) is not redundant with `:870`'s post-acquire re-check.

        `:870` catches a `stop()` landing DURING the promotion and releases
        the lock afterwards — the outcome converges either way. But without
        `:841`, a stopped engine still ISSUES the flock acquire, and
        `timeout=0` means that attempt can WIN: measured, a stopped engine
        took the machine's LIVE lock (0 flock attempts -> 1, and the
        attempt succeeded). A concurrent successor's own `timeout=0`
        acquire then loses to a lock held by an engine that will never
        tick again.

        Pins the PRE-check: a stopped, demoted engine must issue ZERO
        acquire calls, not merely end up dry-run (which :870 alone would
        also produce, so an outcome-only assertion cannot tell the two
        gates apart — this is why the gate previously survived mutation).
        """
        from claude_swap.locking import FileLock

        demoted = harness._make_engine(dry_run=False)
        assert demoted.demoted_from_live, "premise: the harness holds LIVE"
        demoted.stop()  # stopped BEFORE any retry -- the steady-state shape

        attempts: list[bool] = []
        real_acquire = FileLock.acquire

        def counting_acquire(self, *a, **kw):
            attempts.append(True)
            return real_acquire(self, *a, **kw)

        FileLock.acquire = counting_acquire
        try:
            demoted._retry_live_promotion()
        finally:
            FileLock.acquire = real_acquire

        assert attempts == [], (
            f"flock acquire attempts = {len(attempts)} — a stopped engine "
            "must never even TRY the machine's LIVE lock, not merely fail "
            "to keep it"
        )

    def test_a_sigterm_inside_the_switch_does_not_free_live_mid_switch(
        self, harness
    ):
        """`own_tick` skips the wait — it must not also skip the protection.

        A signal handler runs on the main thread inside the frame it
        interrupts, so a SIGTERM delivered while `_perform` is inside
        `switch_to` has `own_tick` True. Waiting there would deadlock, which is
        why the check exists. But returning immediately releases LIVE in the
        middle of a credential rewrite, and a successor — systemd
        `stop`/`start`, or a relaunched `cswap auto` — claims it and acts. That
        is the exact race the lock exists to prevent, reached through the
        handover instead of two TUIs.

        The shipped `own_tick` test only asserts `stop()` was FAST, which this
        satisfies while losing the lock.

        Asserts on the LOCK during the switch, not on `stop()`'s duration: a
        release deferred to the end of the tick is both fast and safe, and only
        a lock check can tell the two apart.
        """
        engine = harness.engine
        held_during_switch: list[bool] = []
        released: list[bool] = []

        class _Lock:
            def release(self):
                released.append(True)

        engine._live_lock = _Lock()

        real_switch = harness.switcher.switch_to

        def switch_then_sigterm(number, **kw):
            engine.stop()          # the handler, on this very thread
            held_during_switch.append(engine._live_lock is not None)
            return real_switch(number, **kw)

        with patch.object(
            harness.switcher, "switch_to", switch_then_sigterm
        ), patch.object(
            harness.switcher, "usage_entries_by_account",
            return_value={
                num: _entry_for(value, harness.clock.now)
                for num, value in {"1": _usage(95), "2": _usage(5)}.items()
            },
        ):
            engine.tick()

        assert held_during_switch == [True], (
            "LIVE was released while switch_to was still running; a successor "
            "can claim it and switch again inside one window"
        )
        assert released == [True], (
            "the deferred release never ran — the lock outlives the engine"
        )

    def test_the_release_warning_survives_a_consumer_that_cannot_take_it(
        self, harness, caplog
    ):
        """The one message that matters is the one that could not be delivered.

        When the ceiling expires, `stop()` releases LIVE anyway and warns that
        two engines may act once. It sent that through `_emit` — which in the
        TUI is `call_from_thread`, refused by Textual from the app's own
        thread, and `autoview._emit_from_thread` swallows the RuntimeError. So
        on the one surface where the timeout actually fires, the warning went
        nowhere.

        Stands the refusal in for Textual's: any consumer that raises. The
        logger has no thread affinity, which is the whole point — a warning
        routed through the UI thread cannot describe a UI thread that is stuck.
        """
        import logging

        engine = harness.engine
        engine.on_event = lambda e: (_ for _ in ()).throw(
            RuntimeError("must run in a different thread")
        )
        released: list[bool] = []

        class _Lock:
            def release(self):
                released.append(True)

        engine._live_lock = _Lock()
        engine._tick_thread_id = -1          # someone else's tick, not ours
        engine._tick_in_flight.clear()

        from claude_swap import autoswitch as autoswitch_mod

        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            with patch.object(autoswitch_mod, "_STOP_SWITCH_WAIT_S", 0.05):
                engine.stop()

        assert released == [True], "premise: the lock was released anyway"

        assert any(
            "two engines may act once" in r.getMessage() for r in caplog.records
        ), (
            f"log records {[r.getMessage() for r in caplog.records]} — the "
            "release warning was swallowed with the consumer's exception, so "
            "an operator has two engines and no explanation"
        )

    def test_the_release_warning_reaches_a_consumer_too(
        self, harness
    ):
        """The sibling test asserts on `caplog` only, so it passed while the
        `_emit` beside the log line was dead code on EVERY surface.

        `_stop.set()` is the FIRST statement of `stop()`, and `_emit` used to
        drop anything without `.reason == "engine-stopped"` — `ErrorEvent` has
        no `.reason` at all. So `cswap auto --json` got no `error` record for
        the one condition where two engines can act at once, which is the
        single most important thing this engine can tell an operator.

        The log line matters for the TUI (Textual refuses `call_from_thread`
        from the app's own thread); the event matters for every consumer that
        is not the TUI. Both, not either.
        """
        import logging

        from claude_swap import autoswitch as autoswitch_mod

        engine = harness.engine
        released: list[bool] = []

        class _Lock:
            def release(self):
                released.append(True)

        engine._live_lock = _Lock()
        engine._tick_thread_id = -1          # someone else's tick, not ours
        engine._tick_in_flight.clear()
        harness.events.clear()

        with patch.object(autoswitch_mod, "_STOP_SWITCH_WAIT_S", 0.05):
            engine.stop()

        assert released == [True], "premise: the ceiling expired"
        assert any(
            isinstance(e, ErrorEvent) and "two engines may act once" in e.message
            for e in harness.events
        ), (
            f"events {[type(e).__name__ for e in harness.events]} — a JSONL "
            "consumer gets no `error` record for the one condition where two "
            "engines can act at once"
        )

    def test_the_release_warning_does_not_touch_the_workers_emit_flag(
        self, harness
    ):
        """`_emit_in_flight` belongs to the WORKER, and it has no refcount.

        Routing `stop()`'s own ceiling warning through `_emit` set the flag
        on this thread and cleared it in `finally` — so a worker that entered
        `on_event` in the meantime came back to a cleared flag, and the next
        `stop()` waited the whole ceiling instead of taking the exemption
        that exists to stop the TUI deadlocking.
        """
        from claude_swap import autoswitch as autoswitch_mod

        engine = harness.engine
        seen: list[bool] = []
        original = engine.on_event

        def watch(event):
            seen.append(engine._emit_in_flight.is_set())
            original(event)

        engine.on_event = watch

        class _Lock:
            def release(self):
                pass

        engine._live_lock = _Lock()
        engine._tick_thread_id = -1
        engine._tick_in_flight.clear()
        harness.events.clear()

        with patch.object(autoswitch_mod, "_STOP_SWITCH_WAIT_S", 0.05):
            engine.stop()

        assert seen, "premise: the ceiling expired and the warning was emitted"
        assert not any(seen), (
            "stop() marked the worker's emit flag while delivering its own "
            "message, so its finally clears a flag it never owned"
        )

    def test_stop_on_the_ui_thread_does_not_block_on_the_worker(self, harness):
        """`own_tick` covers ONE thread. The TUI has two, and it is the shape.

        The sibling test drives emit and `stop()` on the SAME thread, which is
        the SIGTERM shape: the handler runs inside the frame it interrupts, so
        `own_tick` is True and the wait is skipped. In the TUI the tick runs on
        a Textual worker and `stop()` is called from `on_unmount` /
        `_restart_engine` on the UI thread — `_tick_thread_id` is the worker's,
        `own_tick` is False, and `stop()` waits.

        Meanwhile the worker is inside `_emit` → `call_from_thread`, which
        blocks it until the UI thread runs the callback. The UI thread is in
        `stop()`. Neither can move, so the wait always runs to the ceiling:
        30s of frozen dashboard on every `l` toggle or screen exit that lands
        mid-emit.

        Not a race — the `_stop` checks added for the freshen loop GUARANTEE an
        emit at the next checkpoint once `_stop` is set, which is the first
        thing `stop()` does.

        Bounds the assertion well under the ceiling: the point is that the UI
        thread does not wait for a worker that is waiting for it, not the exact
        duration. A real `call_from_thread` is stood in for by a barrier with
        the same dependency, so the test needs no Textual app.
        """
        import threading
        import time

        engine = harness.engine
        emitted = threading.Event()
        ui_ran_callback = threading.Event()
        worker_released: list[bool] = []

        def emit_blocks_until_ui_runs_it(event):
            # What `call_from_thread` does: park the worker until the UI
            # thread executes the callback.
            emitted.set()
            ui_ran_callback.wait(10.0)
            worker_released.append(True)

        engine.on_event = emit_blocks_until_ui_runs_it

        def worker():
            with patch.object(
                harness.switcher, "usage_entries_by_account",
                return_value={
                    num: _entry_for(value, harness.clock.now)
                    for num, value in {"1": _usage(95), "2": _usage(5)}.items()
                },
            ):
                engine.tick()

        w = threading.Thread(target=worker, daemon=True)
        w.start()
        assert emitted.wait(10.0), "premise: the worker reached an emit"
        assert engine._tick_thread_id not in (None, threading.get_ident()), (
            "premise: the tick is on the OTHER thread, so own_tick is False"
        )

        t0 = time.monotonic()
        engine.stop()                       # the UI thread
        blocked = time.monotonic() - t0

        ui_ran_callback.set()               # the UI thread gets back to work
        w.join(10.0)

        assert blocked < 1.0, (
            f"stop() held the UI thread {blocked:.2f}s while the worker was "
            "waiting on that same thread to run its callback — the dashboard "
            "is frozen for the whole ceiling"
        )

    def test_stop_rereads_the_emit_gate_it_checked_before_waiting(
        self, harness
    ):
        """The sibling above waits for the emit BEFORE calling `stop()`, so it
        only ever drives the state the one-shot check already handled.

        The uncovered case is the ordinary one: `stop()` arrives while the
        worker is mid-tick and NOT yet emitting — inside a usage fetch, say.
        The gate reads clear, the wait is entered, and only then does the
        worker reach `_emit` and park on this thread. From that moment the
        wait cannot be satisfied by anything, because the thread the worker
        needs is the one sitting in it. A single read of the gate cannot see
        that; the wait has to re-read it.

        `_stop.set()` is the first statement of `stop()` and `_emit` has no
        stop gate by design, so an emit after the check is guaranteed on every
        path rather than being a narrow race.
        """
        import threading
        import time

        engine = harness.engine
        in_tick = threading.Event()
        release_worker = threading.Event()
        ui_ran_callback = threading.Event()

        def emit_blocks_until_ui_runs_it(event):
            ui_ran_callback.wait(10.0)

        engine.on_event = emit_blocks_until_ui_runs_it

        def parked_usage(*_a, **_kw):
            # The tick is underway and has emitted nothing yet.
            in_tick.set()
            release_worker.wait(10.0)
            return {
                num: _entry_for(value, harness.clock.now)
                for num, value in {"1": _usage(95), "2": _usage(5)}.items()
            }

        def worker():
            with patch.object(
                harness.switcher, "usage_entries_by_account", parked_usage
            ):
                engine.tick()

        w = threading.Thread(target=worker, daemon=True)
        w.start()
        assert in_tick.wait(10.0), "premise: the worker reached the tick body"
        assert not engine._emit_in_flight.is_set(), (
            "premise: nothing has emitted yet, so the gate reads clear"
        )
        assert engine._tick_thread_id not in (None, threading.get_ident()), (
            "premise: the tick is on the OTHER thread, so own_tick is False"
        )

        # Let the worker reach its emit only AFTER stop() is already waiting.
        threading.Timer(0.3, release_worker.set).start()

        t0 = time.monotonic()
        engine.stop()
        blocked = time.monotonic() - t0

        ui_ran_callback.set()
        w.join(10.0)

        assert blocked < 5.0, (
            f"stop() held the UI thread {blocked:.2f}s. The gate was clear "
            "when it was read and set a moment later, so a one-shot check "
            "commits the dashboard to the whole ceiling"
        )

    def test_stop_does_not_wait_on_a_tick_that_is_waiting_on_it(
        self, harness
    ):
        """The waiter must not be the thread the tick depends on.

        `_tick_in_flight` was widened from `switch_to` to the whole tick, and
        the whole tick includes `_emit`. In the TUI, `_emit_from_thread` is
        `app.call_from_thread`, which blocks the WORKER until the UI thread
        runs the callback — and `stop()` is called ON that UI thread
        (`_restart_engine`, `on_unmount`). Worker waits for UI, UI waits for
        worker.

        Measured on the shipped 30s ceiling: every `l` toggle or screen exit
        landing mid-emit froze the whole TUI for 30s, then logged "a tick did
        not finish".

        The same shape hangs `cswap auto`: `cli.py` installs `stop()` as the
        SIGTERM handler while `run_loop` is on the main thread, so the handler
        runs INSIDE the tick it interrupts and waits on a flag only that
        thread can set (measured 3.000s against a 3s ceiling).

        A wait whose own thread owns the work is not a wait, it is a
        deadlock — so `stop()` returns immediately when called from the thread
        running the tick.
        """
        import threading
        import time

        engine = harness.engine
        entered = threading.Event()
        elapsed: list[float] = []

        def emit_then_stop(event):
            if entered.is_set():
                return
            entered.set()
            t0 = time.monotonic()
            engine.stop()          # the UI thread, mid-emit
            elapsed.append(time.monotonic() - t0)

        engine.on_event = emit_then_stop
        with patch.object(
            harness.switcher, "usage_entries_by_account",
            return_value={
                num: _entry_for(value, harness.clock.now)
                for num, value in {"1": _usage(95), "2": _usage(5)}.items()
            },
        ):
            engine.tick()

        assert elapsed, "premise: stop() ran from inside the tick"
        assert elapsed[0] < 1.0, (
            f"stop() blocked {elapsed[0]:.2f}s waiting on the very tick whose "
            "thread called it — the TUI freezes and SIGTERM self-deadlocks"
        )

    def test_a_sigterm_deep_in_the_tick_does_not_block_on_its_own_thread(
        self, harness
    ):
        """`not own_tick` in the wait condition is load-bearing and untested.

        The two shipped tests reach `stop()` from inside `_emit` (exempted by
        `_emit_in_flight`) or inside `switch_to` (exempted by
        `_switch_in_flight`). A SIGTERM landing ANYWHERE ELSE in the tick —
        `_freshen_target`'s refresh POST is the widest such window — has all
        three flags in the one state only `own_tick` handles:
        `_tick_in_flight` clear, `_emit_in_flight` clear,
        `_switch_in_flight` False.

        With `not own_tick` removed and the ceiling patched to 2.0s the
        reviewer measured `stop()` blocking the full 2.00s on its own thread
        and then logging the unsafe-release warning. At the shipped 30s
        ceiling that is a 30s `cswap auto` SIGTERM hang followed by an unsafe
        release — the signal handler waiting on a flag only the frame it
        interrupted can set.

        Bounded well under the patched ceiling: the point is that the handler
        does not wait on itself, not the exact duration.
        """
        import time

        from claude_swap import autoswitch as autoswitch_mod

        harness.seed(2, "b@example.com")
        engine = harness.engine
        elapsed: list[float] = []

        def freshen_then_sigterm(number, email):
            # The signal handler runs INSIDE the frame it interrupts, so this
            # is `stop()` on the tick's own thread with no flag exempting it.
            assert not engine._tick_in_flight.is_set(), "premise: tick running"
            assert not engine._emit_in_flight.is_set(), "premise: not emitting"
            assert not engine._switch_in_flight, "premise: not switching"
            t0 = time.monotonic()
            engine.stop()
            elapsed.append(time.monotonic() - t0)
            return "ok"

        engine._freshen_target = freshen_then_sigterm
        entries = {
            num: _entry_for(value, harness.clock.now)
            for num, value in {"1": _usage(99), "2": _usage(5)}.items()
        }
        with patch.object(autoswitch_mod, "_STOP_SWITCH_WAIT_S", 2.0):
            with patch.object(
                harness.switcher, "usage_entries_by_account",
                return_value=entries,
            ):
                engine.tick()

        assert elapsed, "premise: stop() ran from inside the tick"
        assert elapsed[0] < 1.0, (
            f"stop() blocked {elapsed[0]:.2f}s waiting on the tick whose own "
            "thread called it — a SIGTERM to `cswap auto` hangs for the full "
            "ceiling and then releases LIVE unsafely"
        )

    def test_the_freshen_loop_stops_between_candidates(self, harness):
        """The ceiling is a backstop; the loop must not need it.

        `_STOP_SWITCH_WAIT_S` is 30s, and ONE candidate's freshen can serially
        take a consume-lock acquire (10s) + the slot FileLock (10s) + a
        refresh POST (10s). The loop iterates over EVERY candidate, so the
        ceiling sits below a single candidate's worst case, not above a
        tick's. Measured on the shipped ceiling:

            stop() gave up after 30.0s
            successor dry_run=False; predecessor tick still running=True
            predecessor freshened so far ['2'], in total ['2', '3']

        The successor holds LIVE while the predecessor keeps consuming
        one-time grants. Checking `_stop` between candidates bounds it by the
        work rather than by a number.
        """
        import threading

        engine = harness.engine
        entered = threading.Event()
        freshened: list[str] = []
        def spy(number, email):
            freshened.append(number)
            if not entered.is_set():
                entered.set()
                engine.stop()      # the handover happens here
            return "transient"     # keeps the loop moving to the next one

        engine._freshen_target = spy
        entries = {
            num: _entry_for(value, harness.clock.now)
            for num, value in {
                "1": _usage(99), "2": _usage(5), "3": _usage(4),
            }.items()
        }
        with patch.object(
            harness.switcher, "usage_entries_by_account", return_value=entries
        ):
            engine.tick()

        assert len(freshened) == 1, (
            f"freshened {freshened} after stop() — the loop kept POSTing "
            "one-time grants for accounts the successor already owns"
        )

    def test_the_freshen_loop_names_the_stop_not_a_stale_fetch(self, harness):
        """The sibling test asserts `len(freshened) == 1` and passes with the
        gate deleted, because `stop()` flips `dry_run = True` and the NEXT
        iteration took `_perform`'s dry-run branch and returned. The loop was
        bounded by that flip, not by the guard the test names.

        The flip no longer decides it — `_perform` asks `_stop` before it
        reads `dry_run` — so the grants are bounded either way. What the gate
        still owns is the DIAGNOSIS. Reached from consume-first, the next
        iteration hits the staleness check first, and with the gate removed a
        stopped engine reports:

            reasons=['PollEvent', 'stale-usage']

        against `['PollEvent', 'engine-stopped']` unmutated. "usage could not
        be refreshed this tick (backoff or a concurrent poller); retrying"
        sends an operator after a fetch problem that does not exist, for an
        engine that simply stopped.

        Asserts the REASON, which is what the gate decides now, rather than
        the freshen count, which something else already bounds.
        """
        harness.seed(2, "b@example.com")
        harness.seed(3, "c@example.com")
        harness.settings = replace(harness.settings, strategy="consume-first")
        engine = harness._make_engine()
        engine.settings = harness.settings
        harness.engine.stop()          # free LIVE for the engine under test
        engine.dry_run = False

        freshened: list[str] = []

        def spy(number, email):
            freshened.append(number)
            if len(freshened) == 1:
                engine.stop()          # the handover happens here
            return "transient"         # keeps the loop moving to the next one

        engine._freshen_target = spy
        events: list = []
        engine.on_event = events.append
        entries = {
            "1": _entry_for(_usage7(20, 20, _R_LATEST), harness.clock.now),
            "2": _entry_for(_usage7(10, 10, _R_SOON), harness.clock.now),
            # The next candidate's entry is stale — the branch that runs
            # first once the gate is gone.
            "3": _entry_for(
                _usage7(10, 10, _R_LATER), harness.clock.now - 100_000
            ),
        }
        with patch.object(
            harness.switcher, "usage_entries_by_account", return_value=entries
        ):
            engine.tick()

        assert freshened == ["2"], f"premise: the stop landed mid-loop, {freshened}"
        reasons = [e.reason for e in events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["engine-stopped"], (
            f"reasons {reasons} — a stopped engine blamed a stale fetch, "
            "sending an operator after a backoff/poller problem that is not "
            "happening"
        )

    def test_a_stopped_engine_does_not_finish_freshening(self, harness):
        """`_tick_in_flight` guarded only `switch_to`.

        Every other mutation in the tick left the flag SET, so `stop()`
        returned instantly and freed LIVE while the predecessor was still
        inside `_freshen_target` — which POSTs a ONE-TIME refresh grant.
        Measured before the fix:

            stop() returned in 0.000s; successor LIVE=True
            grants the STOPPED engine consumed after handover: ['2', '3']

        `test_a_stopped_engine_stops_MUTATING_not_just_switching` covers a
        tick that STARTS after `stop()`. This covers one already in flight,
        which is the common case: the TUI's `_restart_engine` stops and
        reconstructs in the same call while the worker is mid-tick.
        """
        import threading

        engine = harness.engine
        entered = threading.Event()
        release = threading.Event()
        freshened: list[str] = []
        real = engine._freshen_target

        def blocking_freshen(number, email):
            freshened.append(number)
            entered.set()
            release.wait(5)
            return real(number, email)

        engine._freshen_target = blocking_freshen
        entries = {
            num: _entry_for(value, harness.clock.now)
            for num, value in {"1": _usage(95), "2": _usage(5)}.items()
        }

        def run():
            with patch.object(
                harness.switcher, "usage_entries_by_account", return_value=entries
            ):
                engine.tick()

        worker = threading.Thread(target=run)
        worker.start()
        try:
            assert entered.wait(5), "premise: a freshen is in flight"

            stopped = threading.Event()

            def do_stop():
                engine.stop()
                stopped.set()

            stopper = threading.Thread(target=do_stop)
            stopper.start()
            import time

            time.sleep(0.2)
            assert not stopped.is_set(), (
                "stop() returned while a freshen was in flight — it freed the "
                "LIVE lock and the predecessor went on to POST a one-time "
                "refresh grant for an account the successor now owns"
            )
        finally:
            release.set()
            worker.join(10)
            stopper.join(10)

    def test_the_LIVE_lock_is_not_freed_while_a_switch_is_in_flight(
        self, harness
    ):
        """`stop()` sets the flag AFTER releasing the lock, so the check is stale.

        `_perform` tests `_stop` under the state lock and then calls
        `switch_to` still holding it, which is correct against a successor
        that respects the state lock. But `stop()` releases the LIVE lock
        first, so a successor constructed in the same call — every caller does
        — claims LIVE while the predecessor's switch is still running. Both
        then act, and the at-limit escape skips the cooldown by design, so the
        second undoes the first's choice inside one cooldown window.

        That is the failure the LIVE lock exists to prevent, reached through
        the handover rather than through two TUIs.

        Setting the flag BEFORE the release closes it: any `_perform` that has
        not yet passed its check refuses, and one that has is already inside
        `switch_to` with the successor unable to hold LIVE yet.
        """
        import threading

        engine = harness.engine
        entered = threading.Event()
        release = threading.Event()
        real_switch = harness.switcher.switch_to

        def blocking_switch(number, **kw):
            entered.set()
            release.wait(5)
            return real_switch(number, **kw)

        entries = {
            num: _entry_for(value, harness.clock.now)
            for num, value in {"1": _usage(100), "2": _usage(5)}.items()
        }
        outcome: list = []

        def run():
            with patch.object(harness.switcher, "switch_to", blocking_switch), \
                    patch.object(
                        harness.switcher, "usage_entries_by_account",
                        return_value=entries,
                    ):
                outcome.append(engine.tick())

        worker = threading.Thread(target=run)
        worker.start()
        assert entered.wait(5), "premise: a switch is in flight"

        import time

        stopped = threading.Event()

        def do_stop():
            engine.stop()
            stopped.set()

        stopper = threading.Thread(target=do_stop)
        stopper.start()
        time.sleep(0.2)
        assert not stopped.is_set(), (
            "stop() returned while a switch was in flight — it freed the LIVE "
            "lock without waiting for the state lock the switch holds"
        )
        # A successor built DURING the switch must not hold LIVE. Built after
        # it, taking LIVE is correct — the handover is complete.
        mid = harness._make_engine()
        assert mid.dry_run is True, (
            "a successor constructed while the predecessor's switch was still "
            "running took LIVE — two engines acting, which is what the lock "
            "prevents"
        )
        mid.stop()

        release.set()
        stopper.join(10)
        worker.join(10)

    def test_a_stopped_engine_writes_no_state_at_all(self, harness):
        """The gate has to precede the mutators, not sit among them.

        `_tick_inner` releases recovered quarantines and runs the scheduled
        usage collection — live fetches, refresh POSTs, usage-store and
        poll-plan writes — hundreds of lines before the candidate loop. A gate
        inside that loop stops the last mutation and none of the earlier ones.

        Measured, `stop()` then one `tick()`, with the gate at the loop:

            _release_recovered_quarantines   {"2": {...}} -> {}
            _collect_scheduled_usage         fetched ['1','2','3']
                                             consumed ['2']   (a one-time grant)
            usage store / poll plans         absent -> full rows

        Same harm the loop gate names, reached earlier: a departing engine
        acting for accounts its successor already owns.
        """
        engine = harness.engine
        released: list[str] = []
        collected: list[str] = []
        real_release = engine._release_recovered_quarantines
        real_collect = engine._collect_scheduled_usage

        def spy_release(state):
            released.append("called")
            return real_release(state)

        def spy_collect(*a, **kw):
            collected.append("called")
            return real_collect(*a, **kw)

        engine._release_recovered_quarantines = spy_release
        engine._collect_scheduled_usage = spy_collect
        engine.stop()
        engine.tick()

        assert released == [], "a stopped engine released quarantines"
        assert collected == [], (
            "a stopped engine ran the usage collection — live fetches, refresh "
            "POSTs and store writes for accounts its successor now owns"
        )

    def test_a_stopped_engine_stops_MUTATING_not_just_switching(self, harness):
        """`stop()` releases the LIVE lock synchronously; the tick does not stop.

        No caller joins the worker thread, so an in-flight tick runs to
        completion while its successor legitimately owns LIVE. `_perform`
        checks `_stop` under the state lock and correctly blocks the SWITCH,
        but the tick mutates well before reaching it:

            _freshen_target consults _stop? False   # POSTs a refresh grant
            _quarantine     consults _stop? False   # writes quarantine state
            _perform        consults _stop? True

        So the predecessor can consume a one-time refresh grant, or quarantine
        a slot, on behalf of an engine that has already handed over. The same
        reasoning that put a check in `_perform` applies here — the freshen is
        a mutation, which is why dry-run stops short of it too.
        """
        engine = harness.engine
        freshened: list[str] = []
        real = engine._freshen_target

        def spy(number, email):
            freshened.append(number)
            return real(number, email)

        engine._freshen_target = spy
        engine.stop()          # the successor may now claim LIVE

        with patch.object(
            harness.switcher,
            "usage_entries_by_account",
            return_value={
                num: _entry_for(value, harness.clock.now)
                for num, value in {"1": _usage(95), "2": _usage(5)}.items()
            },
        ):
            engine.tick()

        assert freshened == [], (
            f"a stopped engine freshened {freshened} — it POSTed a one-time "
            "refresh grant for an account its successor now owns"
        )

    def test_demotion_is_announced_once(self, harness):
        second = harness._make_engine()
        events: list = []
        second.on_event = events.append
        entries = {
            num: _entry_for(value, harness.clock.now)
            for num, value in {"1": _usage(10), "2": _usage(10)}.items()
        }
        with patch.object(
            harness.switcher, "usage_entries_by_account", return_value=entries
        ):
            second.tick()
            second.tick()
        warnings = [e for e in events if isinstance(e, ConfigWarningEvent)]
        assert len(warnings) == 1
        assert "already running" in warnings[0].message

    def test_an_explicit_dry_run_engine_never_takes_the_lock(self, temp_home):
        """A dry-run engine must not lock out the LIVE one — otherwise a
        watching TUI would demote the engine that does the work."""
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.engine.stop()
        watcher = h._make_engine(dry_run=True)
        assert watcher.dry_run is True
        assert watcher.demoted_from_live is False
        live = h._make_engine()
        assert live.dry_run is False               # the lock was free

    def test_stop_releases_the_lock_for_the_next_engine(self, harness):
        """The TUI's LIVE/dry-run toggle stops one engine and builds the next
        in the same call; a lock released only by the exiting worker thread
        would make the TUI demote itself."""
        harness.engine.stop()
        successor = harness._make_engine()
        assert successor.dry_run is False
        assert successor.demoted_from_live is False

    def test_the_lock_is_cross_process(self, harness, tmp_path):
        """flock, not a pid file: the guard has to hold against a separate
        process, which is the actual two-TUI shape."""
        import subprocess
        import sys
        import textwrap

        lock_path = harness.switcher.backup_dir / ".auto-live.lock"
        assert lock_path.exists()                  # the winner holds it
        code = textwrap.dedent(f"""
            from pathlib import Path
            from claude_swap.locking import FileLock
            print(FileLock(Path({str(lock_path)!r}), timeout=0).acquire())
        """)
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert out.stdout.strip() == "False", out.stderr


def _seed_healed_strike(h: EngineHarness, num: str, email: str, *, stale: bool = False):
    """A row that trips `_collect_usage_entries`'s `elif` heal branch.

    switcher.py:4063  `_entry_token_dead(...)` -> False: it passes
        `stored_fp=fingerprint(backup)`, which != struckFingerprint, so
        `token_dead` returns False (the fingerprint healed the verdict).
    switcher.py:4065  `elif entry.auth_dead_strikes and entry.token_dead()`
        -> True: auth_dead_strikes == 1 == AUTH_DEAD_STRIKES, and
        `token_dead()` called with NO stored_fp skips the fingerprint check.
    switcher.py:4071  -> `clear_dead_token` -> `_mutate` -> `_write_rows`.
    """
    path = h.switcher._usage_store.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schemaVersion": 2,
        "accounts": {
            num: {
                "email": email,
                "organizationUuid": "",
                "authDeadStrikes": 1,
                "struckFingerprint": "fp-of-a-generation-that-is-gone",
                "consecutiveFailures": 3,
                "lastError": "invalid_grant",
                "backoffUntil": 9e18,
                # stale=True lets a successor's reserve() win the row later.
                "fetchedAt": h.clock.now - (100000.0 if stale else 0.0),
                "lastGood": {"five_hour": {"pct": 10.0}},
            }
        },
    }))
    return path


def _seed_stale_quarantine(h: EngineHarness, num: str, email: str) -> None:
    """Fingerprint no longer matches -> released next tick, which EMITS.
    That emit is the stop's landing site, and it is exempt from stop()'s
    wait by design (autoswitch.py's `_emit_in_flight` exemption)."""
    (h.switcher.backup_dir / "autoswitch_state.json").write_text(json.dumps({
        "schemaVersion": 1,
        "quarantine": {
            num: {
                "email": email,
                "reason": "invalid_grant",
                "at": "2024-01-01T00:00:00Z",
                "refreshTokenFingerprint": "stale-fingerprint",
            }
        },
    }))


def _run_write_probe(harness: EngineHarness, *, with_strike: bool):
    h = harness
    path = h.switcher._usage_store.path
    if with_strike:
        _seed_healed_strike(h, "2", "b@example.com")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schemaVersion": 2,
            "accounts": {
                "2": {
                    "email": "b@example.com",
                    "organizationUuid": "",
                    "authDeadStrikes": 0,          # <- the ONLY difference
                    "consecutiveFailures": 3,
                    "lastError": "invalid_grant",
                    "backoffUntil": 9e18,
                    "fetchedAt": h.clock.now,
                    "lastGood": {"five_hour": {"pct": 10.0}},
                }
            },
        }))
    _seed_stale_quarantine(h, "3", "c@example.com")

    before = json.loads(path.read_text())

    engine = h.engine            # holds the LIVE lock: built first
    assert not engine.dry_run, "probe needs a LIVE engine, not a demoted one"

    seen: list[str] = []
    fetch_calls: list = []
    writes: list[str] = []
    real = h.switcher.usage_entries_by_account
    real_write = h.switcher._usage_store._write_rows

    def spy(fetch=None, **kw):
        fetch_calls.append(set(fetch) if fetch is not None else None)
        return real(fetch=fetch, **kw)          # PASS-THROUGH, not a stub

    def spy_write(rows):
        writes.append("write")
        return real_write(rows)

    def on_event(ev) -> None:
        seen.append(ev.kind)
        if ev.kind == "account-unquarantined":
            engine._stop.set()                  # the stop lands in the emit

    engine.on_event = on_event
    with patch.object(h.switcher, "usage_entries_by_account", side_effect=spy), \
         patch.object(h.switcher._usage_store, "_write_rows", side_effect=spy_write), \
         patch.object(h.switcher, "_run_usage_fetches", return_value={}) as no_net:
        outcome = engine.tick()

    return {
        "outcome": outcome, "events": seen, "fetch_calls": fetch_calls,
        "store_writes": len(writes), "network": no_net.call_count,
        "before": before, "after": json.loads(path.read_text()),
    }


def test_repro_usage_json_written_after_stop(harness: EngineHarness):
    """RED with the gate absent, GREEN with it restored."""
    r = _run_write_probe(harness, with_strike=True)
    b, a = r["before"]["accounts"]["2"], r["after"]["accounts"]["2"]

    # NON-VACUITY. Only facts that hold in BOTH worlds -- whether the
    # collection ran is exactly what the gate decides, so it is printed as a
    # diagnostic below, never asserted as a precondition.
    assert "account-unquarantined" in r["events"], r["events"]   # vector fired
    assert "no-switch" in r["events"], r["events"]               # tick aborted
    assert b["authDeadStrikes"] == 1                             # strike seeded

    print(f"\nA outcome={r['outcome']} events={r['events']}")
    print(f"A usage_entries_by_account calls = {r['fetch_calls']}   "
          f"(gate absent -> [set()], the :1574 pre-read; restored -> [])")
    print(f"A _write_rows calls = {r['store_writes']}   network fetches = {r['network']}")
    print(f"A before: strikes={b['authDeadStrikes']} fp={b['struckFingerprint']!r} "
          f"failures={b['consecutiveFailures']} backoffUntil={b['backoffUntil']}")
    print(f"A after : strikes={a['authDeadStrikes']} fp={a['struckFingerprint']!r} "
          f"failures={a['consecutiveFailures']} backoffUntil={a['backoffUntil']}")
    print(f"A usage.json MUTATED AFTER STOP = {r['before'] != r['after']}")

    assert r["before"] == r["after"], (
        "a stopped engine wrote usage.json -- clear_dead_token ran below the "
        "deleted gate, reached via the pre-read at autoswitch.py:1574 which "
        "sits ABOVE _collect_scheduled_usage's own gate at :1628"
    )


def test_repro_control_no_strike_no_write(harness: EngineHarness):
    """CONTROL: identical vector, no strike to heal -> no writer reached.
    Green in BOTH worlds. If this ever goes red the probe is measuring
    something other than the heal path."""
    r = _run_write_probe(harness, with_strike=False)
    assert "account-unquarantined" in r["events"], r["events"]
    print(f"\nA-CONTROL store_writes={r['store_writes']} "
          f"MUTATED={r['before'] != r['after']}")
    assert r["before"] == r["after"]


# ======================= B: CONSEQUENCE DEMONSTRATION ======================


class TestStoppedEngineDoesNotAct:
    """A stopped engine must not switch, even mid-tick.

    ``stop()`` only asks the loop to exit; a tick already in flight runs to
    completion (Textual's ``run_worker`` is not exclusive, so the old worker
    is never cancelled). Every ``stop()`` caller constructs the successor
    immediately afterwards — ``_restart_engine`` and ``on_unmount`` +
    ``on_mount`` — so without this the predecessor can switch while the
    successor is LIVE, which is the two-engine race the lock exists to stop.
    """

    def test_a_stopped_engine_does_not_switch(self, harness):
        harness.engine.stop()
        outcome = harness.tick_with_usage({"1": _usage(95), "2": _usage(5)})
        assert outcome is TickOutcome.NO_ACTION
        assert harness.active_number() == 1

    def test_a_stop_in_the_refresh_post_does_not_report_a_switch(
        self, harness
    ):
        """Exit 0 = SWITCHED for a switch that never happened.

        The freshen loop's `_stop` gate sits BEFORE `_freshen_target` and
        nothing re-checks between the freshen returning and `_perform`. So a
        `stop()` landing inside the refresh POST — the widest window in the
        loop — finds `_perform`'s first statement `if self.dry_run:`, which
        `stop()` has just flipped True. The engine emits a `dryRun: true`
        SwitchEvent and returns SWITCHED.

        Measured before the fix:

            outcome=SWITCHED exit_code=0 active=1 events=['PollEvent']

        `cli.py:703` is `sys.exit(engine.tick().value)`, so a cron wrapper
        keying on exit 0 records a successful switch that did not occur, the
        one-time grant is already spent, and the successor inherits a burned
        generation. The SwitchEvent that would have said `dryRun: true` never
        arrives either — `_emit`'s stop gate drops it.

        `_EngineStopped` is the shape for this: abandon the tick, do not
        report work.
        """
        harness.seed(2, "b@example.com")
        engine = harness.engine
        assert not engine.dry_run, "premise: this engine is LIVE"

        def freshen_then_sigterm(number, email):
            engine.stop()      # SIGTERM lands inside the refresh POST
            return "ok"        # ... and the POST succeeded

        engine._freshen_target = freshen_then_sigterm
        harness.events.clear()
        entries = {
            num: _entry_for(value, harness.clock.now)
            for num, value in {"1": _usage(99), "2": _usage(5)}.items()
        }
        with patch.object(
            harness.switcher, "usage_entries_by_account", return_value=entries
        ):
            outcome = engine.tick()

        assert harness.active_number() == 1, "premise: nothing switched"
        assert outcome is not TickOutcome.SWITCHED, (
            f"exit {outcome.value} = SWITCHED while the active account is "
            f"still {harness.active_number()} — a cron wrapper records a "
            "switch that did not happen"
        )
        assert any(
            getattr(e, "reason", None) == "engine-stopped"
            for e in harness.events
        ), (
            f"events {[type(e).__name__ for e in harness.events]} — the tick "
            "abandoned itself with no reason line"
        )

    def test_stop_between_freshen_and_perform_reports_no_switch(self, harness):
        """Gate :1702, `_perform`'s own entry re-check, is not
        mutation-covered by anything else in the suite -- deleting it alone
        still leaves every other test passing (measured: full suite green
        with it disabled).

        `test_a_stop_in_the_refresh_post_does_not_report_a_switch` (above)
        lands its `stop()` INSIDE `_freshen_target`, which the freshen
        loop's OWN gate at :1336 catches immediately on the way back out --
        `_perform` is never even reached, so :1702 does nothing there.

        This lands `stop()` strictly AFTER `_freshen_target` returns "ok"
        and the loop's :1336 re-check has already passed -- in the one-
        statement gap between `return self._perform(...)` being called and
        `_perform`'s own first line running. `stop()` flips BOTH `_stop`
        and (moments later, under its own lock) `dry_run = True`; without
        :1702 as the FIRST statement in `_perform`, execution falls straight
        to `if self.dry_run:`, now True, and reports a fake dry-run SWITCHED
        for a switch that never ran on this now-dead engine.
        """
        harness.seed(2, "b@example.com")
        engine = harness.engine
        assert not engine.dry_run, "premise: this engine is LIVE"

        real_perform = engine._perform

        # Forwards whatever `_perform` actually takes. NOT because the arity
        # is unknown here — it is — but because this fake stands in for a
        # method other branches extend (a departure snapshot, a trigger
        # reason), and a fake pinned to today's arity fails with a TypeError
        # the tick's own error handler swallows into an ErrorEvent. That
        # reads as "the stop gate regressed" while the gate is fine, which
        # is the wrong bug to go looking for. `functools.wraps` keeps the
        # signature introspectable for anything that asks.
        @functools.wraps(real_perform)
        def perform_after_stop(*args, **kwargs):
            engine.stop()  # lands in the gap before _perform's own gate
            return real_perform(*args, **kwargs)

        engine._perform = perform_after_stop
        harness.events.clear()
        outcome = harness.tick_with_usage({"1": _usage(99), "2": _usage(5)})

        assert harness.active_number() == 1, "premise: nothing switched"
        assert outcome is not TickOutcome.SWITCHED, (
            f"exit {outcome.value} = SWITCHED while the active account is "
            f"still {harness.active_number()} — a cron wrapper records a "
            "switch that did not happen"
        )
        assert any(
            getattr(e, "reason", None) == "engine-stopped"
            for e in harness.events
        ), (
            f"events {[type(e).__name__ for e in harness.events]} — the tick "
            "abandoned itself with no reason line"
        )

    def test_the_last_candidates_stop_is_diagnosed_as_a_stop(self, harness):
        """C-1: the loop-top gate (:1304) only re-fires on the NEXT
        iteration. With exactly ONE candidate there is no next iteration —
        the loop falls out the bottom into the diagnosis block at
        :1371-1391, which has no gate of its own.

        Measured before the fix, one candidate, stop landing inside
        `_freshen_target` with status "transient":

            outcome=ERROR reason=None
            message='could not freshen any candidate (network?)'

        against the `engine-stopped` NO_ACTION every other stop path
        reports. A `cswap auto --once` SIGTERMed mid-refresh on a
        2-account machine would exit 1 and send the operator to check
        their network for a problem that is not there.
        """
        engine = harness.engine

        def freshen_then_stop(number, email):
            engine.stop()          # the SIGTERM lands inside the refresh POST
            return "transient"     # ... which failed for an unrelated reason

        engine._freshen_target = freshen_then_stop
        harness.events.clear()
        # Only "2" ranks as a candidate: "3" carries no usage entry this
        # tick, so `_rank_candidates` never sees it as known.
        outcome = harness.tick_with_usage({"1": _usage(95), "2": _usage(5)})

        assert outcome is not TickOutcome.ERROR, (
            f"outcome={outcome!r} — a stopped engine reported ERROR "
            "('could not freshen any candidate (network?)') for a tick "
            "that simply stopped"
        )
        reasons = [
            getattr(e, "reason", None) for e in harness.events
            if isinstance(e, NoSwitchEvent)
        ]
        assert reasons == ["engine-stopped"], (
            f"reasons={reasons} — a stopped engine blamed a network "
            "problem that never happened"
        )

    def test_the_last_candidates_stop_writes_no_quarantine(self, harness):
        """C-1's second harm: `_quarantine` runs on the LAST candidate's
        status before the loop-top gate ever gets a chance to fire again.

        Measured before the fix, one candidate, stop landing inside
        `_freshen_target` with status "invalid_grant":

            {'2': {'email': 'b@example.com', 'reason': 'invalid_grant', ...}}

        written to disk AFTER `stop()` released LIVE — contradicting
        `test_a_stopped_engine_writes_no_state_at_all`'s invariant one
        branch over. The successor inherits a quarantine its predecessor
        wrote after handover: an account barred by a process that no
        longer owns the decision.
        """
        engine = harness.engine

        def freshen_then_stop(number, email):
            engine.stop()
            return "invalid_grant"

        engine._freshen_target = freshen_then_stop
        harness.events.clear()
        harness.tick_with_usage({"1": _usage(95), "2": _usage(5)})

        assert harness.state().get("quarantine", {}) == {}, (
            f"state={harness.state()} — a stopped engine quarantined an "
            "account on behalf of a successor that already owns LIVE"
        )

    def test_stop_between_candidates_freshens_no_further_candidate(
        self, harness
    ):
        """Gate :1304 (the loop-top gate, BETWEEN candidates) is not
        mutation-covered by anything else in the suite -- deleting it alone
        still leaves every other test passing. Every existing stop test in
        this loop lands the stop INSIDE `_freshen_target` for the LAST
        candidate (C-1's shape), which the OTHER gate at :1336 catches
        immediately -- :1304 never even gets exercised.

        `engine._stop.set()` directly, NOT `engine.stop()`: `stop()` sets
        `_stop` first and only THEN flips `dry_run = True` under its own
        lock, a two-statement window a concurrent reader can observe between
        them. Calling `stop()` here collapses that window in-process --
        `dry_run` flips too, and the loop's OWN `if self.dry_run:` check
        (right after this gate) would mask a deleted :1304, freshening
        nothing regardless of the gate. `_stop.set()` alone reproduces
        exactly the window :1304 exists to close: `_stop` observed True,
        `dry_run` still False.

        Three candidates, most headroom first ("3" then "2"): "3"'s freshen
        fails with `invalid_grant`, quarantines (stop lands here), then
        `continue`s to the loop top for "2". With the gate present,
        `_freshen_target` is called once, for "3" only. Deleted, the loop
        proceeds to freshen "2" too -- a network POST for a successor that
        already owns LIVE.
        """
        harness.seed(4, "d@example.com")
        engine = harness.engine
        calls: list[str] = []
        real_quarantine = engine._quarantine

        def freshen(number, email):
            calls.append(number)
            return "invalid_grant" if number == "3" else "ok"

        def quarantine_then_stop(number, email, reason):
            real_quarantine(number, email, reason)
            engine._stop.set()  # lands right before the loop's `continue`

        engine._freshen_target = freshen
        engine._quarantine = quarantine_then_stop
        harness.events.clear()
        assert not engine.dry_run, "premise: this engine is LIVE"
        harness.tick_with_usage({
            "1": _usage(95), "2": _usage(50), "3": _usage(10), "4": _usage(80),
        })

        assert calls == ["3"], (
            f"_freshen_target calls={calls} -- a stopped engine freshened a "
            "candidate past the one that triggered the stop, for a "
            "successor that already owns LIVE"
        )

    def test_a_stop_before_the_collection_writes_no_usage_store_row(
        self, harness
    ):
        """The deleted gate, restored at :1008, was NOT redundant, and the
        census that called it redundant asked the wrong question.

        The reasoning was "nothing between here and `_collect_scheduled_usage`'s
        own gate emits or mutates". That gate is at :1628, but the method's
        FIRST statement is at :1574 --

            pre = self.switcher.usage_entries_by_account(fetch=set())

        -- which runs ABOVE it. `fetch=set()` makes it no-NETWORK, and the
        comment above the inner gate says exactly that ("touch no network and
        stay"). No-network is not no-write: it reaches
        `switcher.py:_collect_usage_entries` -> `usage_store.clear_dead_token`,
        which does `row["claimId"] = None` and `self._mutate(...)` -> a real
        `_write_rows`.

        Nulling `claimId` is not cosmetic. `record()` fences on it
        (`row.get("claimId") != expected -> continue`), so a stopped
        predecessor's write DISCARDS a successor's in-flight fetch.

        The stop is injected inside the unquarantine emit because that path is
        exempt from `stop()`'s wait by design -- which is precisely where the
        deleted gate used to catch it.
        """
        engine = harness.engine
        # A REAL strike, not `tick_with_usage`'s canned entries: reaching
        # `clear_dead_token` needs `auth_dead_strikes and token_dead()`,
        # which the plain harness never sets, so a spy on `_write_rows` was
        # vacuous when the fetch that reaches it was stubbed out too --
        # zero calls whether or not the gate exists. `tick_with_usage`
        # PATCHES OUT `usage_entries_by_account` (returns canned entries
        # directly), so it never reaches `switcher._collect_usage_entries`
        # at all; this drives `engine.tick()` with a PASS-THROUGH spy
        # instead, the same shape the real repro
        # (`test_repro_usage_json_written_after_stop`, above) uses.
        _seed_healed_strike(harness, "2", "b@example.com")
        _seed_stale_quarantine(harness, "3", "c@example.com")

        writes: list[str] = []
        store = engine.switcher._usage_store
        real_write = store._write_rows

        def spy_write(rows):
            writes.append("_write_rows")
            return real_write(rows)

        store._write_rows = spy_write

        real_release = engine._release_recovered_quarantines

        def release_then_stop(state):
            out = real_release(state)
            engine.stop()       # lands where the deleted gate used to sit
            return out

        engine._release_recovered_quarantines = release_then_stop
        harness.events.clear()
        with patch.object(harness.switcher, "_run_usage_fetches", return_value={}):
            engine.tick()

        assert writes == [], (
            f"a stopped engine ran {writes} -- a usage-store write for a "
            "successor that already owns LIVE; clear_dead_token nulls "
            "claimId, the field record() fences on"
        )

        # Acceptance control (MINOR-2): the same vector, engine NOT stopped,
        # must actually reach `_write_rows` -- otherwise `writes == []`
        # above is vacuous and a later change that removes the write from
        # this path would pass silently with no signal. `engine` above
        # already released the machine's LIVE lock in `stop()`, but a fresh
        # `dry_run=True` engine avoids depending on that release timing --
        # `dry_run` does not gate the collector's heal-write path, only
        # whether `_perform` switches an account, so this is still the same
        # vector under test.
        _seed_healed_strike(harness, "2", "b@example.com")
        _seed_stale_quarantine(harness, "3", "c@example.com")
        control_engine = harness._make_engine(dry_run=True)
        control_writes: list[str] = []
        control_real_write = store._write_rows

        def control_spy_write(rows):
            control_writes.append("_write_rows")
            return control_real_write(rows)

        store._write_rows = control_spy_write
        with patch.object(
            harness.switcher, "_run_usage_fetches", return_value={}
        ):
            control_engine.tick()
        assert control_writes != [], (
            "acceptance control: the NOT-stopped engine ran 0 "
            "_write_rows calls on this same vector -- the assertion above "
            "would be vacuous"
        )

    def test_stop_inside_phase1_fetch_blocks_the_escalation_refetch(
        self, harness
    ):
        """Gate :1676 (escalation refetch) is not mutation-covered by
        anything else in the suite — deleting it alone still leaves every
        other test passing.

        A stop landing inside the phase-1 collection fetch surfaces on the
        way back from that call, before `_collect_scheduled_usage` has
        decided whether to escalate. Measured (one candidate, active near
        the escalation band):

            gate present:  network_fetches == [['2']]
            gate deleted:  network_fetches == [['2'], ['1', '2', '3']]

        A stopped engine would issue a full 3-account fetch for a
        successor that already owns LIVE — exactly the harm the :1590
        comment above the phase-1 gate exists to prevent, one call later.
        """
        engine = harness.engine
        network_fetches: list[list[str]] = []
        canned = {
            num: _entry_for(value, harness.clock.now)
            for num, value in {
                "1": _usage(80), "2": _usage(10), "3": _usage(10),
            }.items()
        }

        def spy(fetch=None, **kw):
            if fetch:
                network_fetches.append(sorted(fetch))
                if len(network_fetches) == 1:
                    engine.stop()  # SIGTERM lands inside the phase-1 fetch
            return canned

        with patch.object(
            harness.switcher, "usage_entries_by_account", side_effect=spy
        ):
            engine.tick()

        assert network_fetches == [["2"]], (
            f"network_fetches={network_fetches} — a stopped engine issued "
            "a full candidate refetch for a successor that already owns "
            "LIVE"
        )

    def test_stop_inside_phase1_fetch_blocks_the_consume_first_recheck(
        self, harness
    ):
        """Gate :1196 (consume-first two-phase-commit refetch) is not
        mutation-covered by anything else in the suite either — deleting it
        alone still leaves every other test passing. The sibling test above
        pins :1676; this pins the OTHER unmutated gate the same measurement
        names.

        `test_the_freshen_loop_names_the_stop_not_a_stale_fetch` pins the
        same invariant one branch over, but needs the switch-worthy
        candidate to be freshened first — a stop landing this early, before
        `_rank_candidates` even runs provisionally, is invisible to it.

        A stop landing inside `_collect_scheduled_usage`'s OWN phase-1
        fetch is far below the escalation band here (so that collection
        returns normally without ever reaching :1676), and surfaces only
        when the two-phase commit re-checks before spending its own
        refetch. Measured (active well below threshold, one sooner-resetting
        candidate):

            gate present:  network_fetches == [['2']]
            gate deleted:  network_fetches == [['2'], ['1', '2', '3']]
        """
        harness.settings = replace(harness.settings, strategy="consume-first")
        engine = harness._make_engine()
        engine.settings = harness.settings
        harness.engine.stop()  # free LIVE for the engine under test
        engine.dry_run = False

        network_fetches: list[list[str]] = []
        canned = {
            "1": _entry_for(_usage7(20, 20, _R_LATER), harness.clock.now),
            "2": _entry_for(_usage7(10, 10, _R_SOON), harness.clock.now),
            "3": _entry_for(_usage7(10, 10, _R_LATEST), harness.clock.now),
        }

        def spy(fetch=None, **kw):
            if fetch:
                network_fetches.append(sorted(fetch))
                if len(network_fetches) == 1:
                    engine.stop()  # SIGTERM lands inside the phase-1 fetch
            return canned

        with patch.object(
            harness.switcher, "usage_entries_by_account", side_effect=spy
        ):
            engine.tick()

        assert network_fetches == [["2"]], (
            f"network_fetches={network_fetches} — a stopped engine issued "
            "a full candidate refetch for a successor that already owns "
            "LIVE"
        )

    def test_stop_is_diagnosed_before_the_unmanaged_active_advice(
        self, temp_home
    ):
        """Gate :934 sits before `current_account_number()` is even read, so
        it is the only thing that makes a stopped engine report
        `engine-stopped` on a live-but-unmanaged login — deleting it alone
        still leaves the whole suite green.

        Without it, `current` comes back None (unmanaged, not absent) and
        the tick falls all the way through to `has_live_login()`'s advice
        branch, which knows nothing about `_stop`. Measured:

            gate present:  reasons == ['engine-stopped']
            :934 deleted:  reasons == [None, 'unmanaged-active-account']

        A SIGTERM landing before this tick even starts would tell the
        operator to run `cswap --add-account` instead of saying the engine
        simply stopped.
        """
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.make_live("unmanaged@example.com", 99)  # not in sequence -> unmanaged
        h.engine.stop()

        outcome = h.engine.tick()

        assert outcome is TickOutcome.NO_ACTION
        reasons = [
            getattr(e, "reason", None) for e in h.events
            if isinstance(e, NoSwitchEvent)
        ]
        assert reasons == ["engine-stopped"], (
            f"reasons={reasons} — a stopped engine advised "
            "'cswap --add-account' instead of naming the stop"
        )

    def test_every_event_class_survives_a_stop(self, harness):
        """`_emit`'s stop gate exempted `.reason == "engine-stopped"`.

        Only 3 of the 9 event classes HAVE a `.reason` field, so the other 6
        were silently dropped once `_stop` was set — including the two emitted
        on paths that run after `stop()`: `SwitchEvent` (the dry-run switch
        above) and `ErrorEvent` (the "two engines may act once" warning, which
        `stop()` emits AFTER its own `_stop.set()`).

        A per-class opt-in is the defect one layer down: the next event class
        will not be on the list. This asserts the property structurally —
        `_emit` delivers what it is given — so a NEW class is safe by default
        rather than by remembering to update anything. The coverage assert
        below fails loudly if a class is added and not listed, instead of the
        class being quietly dropped at runtime.

        Delivering is the safe default: the gate was censoring the NARRATION
        of a stopped engine, which never stopped it acting — it only removed
        the evidence that it had.
        """
        import inspect

        from claude_swap import autoswitch as m

        samples = [
            m.AutoSwitchEvent(),
            m.PollEvent(active=None, headroom={}, threshold=80.0),
            m.SwitchEvent(trigger="proactive", from_ref=None, to_ref=None),
            m.NoSwitchEvent(reason="cooldown"),
            m.QuarantineEvent(number="2", email="b@example.com", reason="x"),
            m.UnquarantineEvent(number="2", email="b@example.com"),
            m.AllExhaustedEvent(earliest_reset_at=None),
            m.SleepEvent(seconds=1.0, until="2024-01-01T00:00:00Z"),
            m.ErrorEvent(message="two engines may act once"),
            m.ConfigWarningEvent(message="now LIVE"),
        ]
        discovered = {
            name
            for name, obj in vars(m).items()
            if inspect.isclass(obj)
            and name.endswith("Event")
            and issubclass(obj, m.AutoSwitchEvent)
        }
        assert {type(s).__name__ for s in samples} == discovered, (
            f"event classes {discovered - {type(s).__name__ for s in samples}}"
            " are not exercised here — a class `_emit` has never been proven "
            "to deliver"
        )

        engine = harness.engine
        engine.stop()
        assert engine._stop.is_set(), "premise: the engine is stopped"
        harness.events.clear()
        for event in samples:
            engine._emit(event)

        dropped = [
            type(s).__name__
            for s in samples
            if not any(e is s for e in harness.events)
        ]
        assert dropped == [], (
            f"{len(dropped)} of {len(samples)} event classes never reached "
            f"the consumer after stop(): {dropped}"
        )

    def test_stopping_mid_tick_aborts_the_switch(self, harness):
        """The real shape: the tick has already decided and is entering
        _perform when the user toggles the mode or leaves the screen."""
        real_lock = harness.engine._state_lock

        def stop_then_lock(*a, **kw):
            harness.engine.stop()          # the user toggles / leaves the screen
            return real_lock(*a, **kw)

        with patch.object(
            harness.engine, "_state_lock", side_effect=stop_then_lock
        ):
            with patch.object(harness.switcher, "switch_to") as sw:
                outcome = harness.tick_with_usage(
                    {"1": _usage(95), "2": _usage(5)}
                )
        sw.assert_not_called()
        assert outcome is TickOutcome.NO_ACTION
        assert harness.active_number() == 1

    def test_next_delay_after_a_mid_tick_stop_writes_no_usage_store_row(
        self, harness
    ):
        """A third `fetch=set()` door, outside `tick()` entirely.

        `run_loop` calls `_next_delay(outcome)` AFTER `tick()` returns, and
        `_next_delay` -> `_respect_poll_plan` makes its OWN
        `usage_entries_by_account(fetch=set())` call at :2089 -- none of
        `tick()`'s nine `_stop` checkpoints (including the :1008 gate this
        defect family already fixed once) sit anywhere near it, because it
        is not inside `tick()` at all.

        The stop must land MID-TICK, in the `account-unquarantined` emit
        (exempt from `stop()`'s wait by design), not after a completed tick:
        a completed tick already healed the strike, and `_next_delay` would
        find nothing left to do -- proving nothing about this call site.
        `tick()` then raises `_EngineStopped` and returns NO_ACTION with the
        strike still present, exactly as `run_loop` drives it.

        Driven with a PASS-THROUGH spy on `usage_entries_by_account`, not a
        stub: `tick_with_usage` patches that method out entirely, which is
        why the sibling test at :1008 could not see this class of defect
        either (Minor-1 in the same review).
        """
        _seed_healed_strike(harness, "2", "b@example.com")
        _seed_stale_quarantine(harness, "3", "c@example.com")
        path = harness.switcher._usage_store.path
        before = json.loads(path.read_text())

        engine = harness.engine
        assert not engine.dry_run, "premise: this engine is LIVE"

        writes: list[str] = []
        store = engine.switcher._usage_store
        real_write = store._write_rows

        def spy_write(rows):
            writes.append("_write_rows")
            return real_write(rows)

        store._write_rows = spy_write

        def on_event(ev) -> None:
            if ev.kind == "account-unquarantined":
                engine._stop.set()  # SIGTERM lands mid-tick, same site the
                                     # in-tree repro for :1008 uses

        engine.on_event = on_event
        with patch.object(
            harness.switcher, "_run_usage_fetches", return_value={}
        ):
            outcome = engine.tick()
            # run_loop's own next step -- outside every `tick()` checkpoint.
            engine._next_delay(outcome)

        after = json.loads(path.read_text())
        b, a = before["accounts"]["2"], after["accounts"].get("2", {})
        assert writes == [], (
            f"a stopped engine's _next_delay -> _respect_poll_plan wrote "
            f"{writes} to usage.json -- clear_dead_token nulls claimId, the "
            f"field record() fences on, for a successor that already owns "
            f"LIVE. strikes {b.get('authDeadStrikes')} -> "
            f"{a.get('authDeadStrikes')}, claimId {b.get('claimId')!r} -> "
            f"{a.get('claimId')!r}"
        )

        # Acceptance control (MINOR-2): the same vector, engine NOT stopped,
        # must actually reach `_write_rows` -- otherwise `writes == []`
        # above is vacuous and a later change that removes the write from
        # this path would pass silently with no signal. `engine` above
        # already holds the machine's LIVE lock and stays stopped (`_stop`
        # is a one-shot flag), so the control uses a fresh `dry_run=True`
        # engine on the SAME switcher/store instead of contending for that
        # lock -- `dry_run` does not gate the collector's heal-write path,
        # only whether `_perform` switches an account, so this is still the
        # same vector under test.
        _seed_healed_strike(harness, "2", "b@example.com")
        _seed_stale_quarantine(harness, "3", "c@example.com")
        control_engine = harness._make_engine(dry_run=True)
        control_writes: list[str] = []
        control_real_write = store._write_rows

        def control_spy_write(rows):
            control_writes.append("_write_rows")
            return control_real_write(rows)

        store._write_rows = control_spy_write
        with patch.object(
            harness.switcher, "_run_usage_fetches", return_value={}
        ):
            control_outcome = control_engine.tick()
            control_engine._next_delay(control_outcome)
        assert control_writes != [], (
            "acceptance control: the NOT-stopped engine ran 0 "
            "_write_rows calls on this same vector -- the assertion above "
            "would be vacuous"
        )

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


class TestDisabledActiveAccount:
    """A DISABLED account the engine is sitting on must be left at once.

    `set_account_disabled`'s own docstring already promises it: "the
    auto-switch engine ... skip[s] disabled slots".
    `switchable_account_numbers()` honours that for CANDIDATES; nothing
    applies it to `current`. Reported and measured: a disabled, metered account
    with no 5h/7d window held the active slot while the engine reported
    `active-usage-unknown 1/3 before failover`, and it kept being billed for as
    long as it sat there. `autoswitch.py` contains ZERO references to `disabled`
    (control: switcher.py 36, cli.py 3, so the zero is a real absence rather
    than a broken grep).
    """

    def test_disabled_active_leaves_even_when_its_usage_reads_low(self, harness):
        """The case no gate can reach: the row reads FINE and reads LOW.

        `unhealthy_ticks` cannot help here — nothing is unhealthy. The engine
        reports below-threshold and parks on a slot the user withdrew from
        rotation, indefinitely.
        """
        harness.switcher.set_account_disabled("1", True)
        outcome = harness.tick_with_usage({
            "1": _usage(50), "2": _usage(40), "3": _usage(10),
        })
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert outcome is TickOutcome.SWITCHED, (
            f"parked on a disabled active; no-switch reasons={reasons}"
        )
        assert harness.active_number() == 3, "must land on the best candidate"

    def test_disabled_active_does_not_route_through_the_transient_gate(self, harness):
        """The measured acct7 shape: disabled AND no readable window.

        `unhealthy_ticks` is for TRANSIENT unreadability (network, lock
        contention, a failed refresh). `disabled` is a deterministic fact read
        from our own sequence.json, and an account with no quota window is
        unreadable permanently — so the 3-tick wait is guaranteed waste. At the
        TUI's measured ~5-minute cadence that is ~15 minutes of spending an
        account the user asked auto not to use.
        """
        harness.switcher.set_account_disabled("1", True)
        outcome = harness.tick_with_usage({
            "1": None, "2": _usage(40), "3": _usage(10),
        })
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert "active-usage-unknown" not in reasons, (
            f"disabled must not spend the transient gate; reasons={reasons}"
        )
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_control_an_ENABLED_active_still_obeys_the_threshold(self, harness):
        """CONTROL. Without it, a fix that switches on every tick would pass
        both tests above and break the whole policy."""
        outcome = harness.tick_with_usage({
            "1": _usage(50), "2": _usage(40), "3": _usage(10),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert harness.active_number() == 1


class TestDisableMessageMatchesTheNewBehaviour:
    """`cswap disable` told the user the OLD contract in so many words.

    Before `disabled-active` existed, disabling the active slot printed "it
    stays live until you switch away; it just won't be an automatic switch
    target" — accurate then, and a promise the engine now breaks on its next
    tick. A message that describes behaviour the code no longer has is worse
    than no message: it is the reason the reporter thought auto was broken.
    """

    def test_disabling_the_active_slot_does_not_promise_it_stays(
        self, harness, capsys
    ):
        harness.switcher.set_account_disabled("1", True)
        out = capsys.readouterr().out
        # The contract, not a substring. "stays live until you switch away" is
        # still TRUE with no engine running, and saying so is useful — the old
        # message's defect was stating it UNCONDITIONALLY, which its distinctive
        # tail is the marker for. A first version of this test forbade the
        # phrase outright and failed a message that was already correct.
        assert "it just won't be an automatic switch target" not in out, (
            "still the unconditional pre-disabled-active promise:\n" + out
        )
        assert "disabled-active" in out, (
            "the active case must name the trigger that will move off it:\n" + out
        )
        assert "next tick" in out, (
            "must say WHEN, or 'auto will move' reads as someday:\n" + out
        )

    def test_control_disabling_a_NON_active_slot_says_nothing_about_moving(
        self, harness, capsys
    ):
        """CONTROL: the notice is scoped to the ACTIVE slot. Without this, a
        message printed unconditionally would satisfy the test above."""
        harness.switcher.set_account_disabled("2", True)
        out = capsys.readouterr().out
        assert "Disabled Account-2" in out
        assert "active account" not in out, out


class TestDisabledActiveReviewFindings:
    """Three gaps a reviewer found in the first cut of `disabled-active`.

    Each was MEASURED by the reviewer, not inferred, and each is the kind that
    survives a green suite: the API-key gate returns before the new branch, the
    escalation collector was never told about the new trigger, and the trigger
    STRING — the whole load-bearing argument for a new name — was pinned by
    nothing at all.
    """

    @staticmethod
    def _mark_api_key(harness, num: int) -> None:
        data = harness.switcher._get_sequence_data()
        data["accounts"][str(num)]["kind"] = "api_key"
        harness.switcher._write_json(harness.switcher.sequence_file, data)

    def test_a_disabled_API_KEY_active_is_left_too(self, harness):
        """The API-key gate returns NO_ACTION BEFORE the disabled branch.

        `include_api_key_accounts` governs whether an API-key account may be a
        switch TARGET. It must not govern whether the engine may LEAVE one —
        and `cswap disable` has just promised the user it will.
        """
        self._mark_api_key(harness, 1)
        harness.switcher.set_account_disabled("1", True)
        outcome = harness.tick_with_usage({
            "1": None, "2": _usage(40), "3": _usage(10),
        })
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert "active-api-key" not in reasons, (
            f"stranded on a disabled API-key active; reasons={reasons}"
        )
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_control_an_ENABLED_api_key_active_is_still_left_alone(self, harness):
        """CONTROL. The API-key gate must keep working for enabled accounts —
        without this, deleting the gate entirely would pass the test above."""
        self._mark_api_key(harness, 1)
        outcome = harness.tick_with_usage({
            "1": None, "2": _usage(40), "3": _usage(10),
        })
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["active-api-key"]
        assert outcome is TickOutcome.NO_ACTION
        assert harness.active_number() == 1

    def test_it_escalates_the_candidate_fetch_before_choosing(self, harness):
        """`disabled-active` must not decide on a stale candidate snapshot.

        The module documents the invariant at `_collect_scheduled_usage`:
        at-limit, proactive and ordinary failover "never run on the
        pre-escalation snapshot — those triggers imply the escalation
        condition". `escalate` keys only on the ACTIVE row's headroom, so a
        disabled active reading comfortably below the band satisfies neither
        leg and the switch is decided on candidate data up to
        CANDIDATE_MAX_INTERVAL_S old. Measured by the reviewer: it switched
        onto an account that was never fetched that tick.
        """
        harness.switcher.set_account_disabled("1", True)
        fetch_sets: list[set] = []
        entries = {
            n: _entry_for(v, harness.clock.now)
            for n, v in {"1": _usage(50), "2": _usage(40), "3": _usage(10)}.items()
        }

        def spying(*args, **kwargs):
            # Record what the tick ASKED to refresh, then answer with the canned
            # rows. A first version wrapped the real collector, which has no
            # network here — it failed BLOCKED on "fetch failed", i.e. for the
            # wrong reason entirely.
            fetch_sets.append(set(kwargs.get("fetch") or ()))
            return entries

        with patch.object(
            harness.switcher, "usage_entries_by_account", side_effect=spying
        ):
            outcome = harness.engine.tick()

        every = set().union(*fetch_sets) if fetch_sets else set()
        assert "3" in every, (
            "chose a candidate it never refreshed this tick; "
            f"fetch sets were {fetch_sets}"
        )
        assert outcome is TickOutcome.SWITCHED

    def test_the_trigger_string_itself_is_pinned(self, harness):
        """Renaming the trigger to "at-limit" passed all 2248 tests.

        The name is load-bearing precisely because of what it is NOT in: every
        downstream gate keys on `trigger in ("proactive", "consume-first")`, and
        "at-limit" would re-enter tuples the design says it must stay out of.
        The CLI notice also tells the user to look for this exact string, so the
        two can desynchronise silently.
        """
        harness.switcher.set_account_disabled("1", True)
        harness.tick_with_usage({
            "1": _usage(50), "2": _usage(40), "3": _usage(10),
        })
        switches = [e for e in harness.events if e.kind == "switch"]
        assert [e.trigger for e in switches] == ["disabled-active"]
        assert harness.state()["leftTrigger"] == "disabled-active"


class TestReEnableIsNotBarredByTheNoReturnBar:
    """Re-enabling a slot the engine left must let it come back.

    The COMMON shapes, outside the band. Something WAS broken here, narrowly:
    see `TestDisabledActiveDepartureDoesNotBarTheReEnabledSlot` for the band
    (active_h 7..10, a peer inside a window at most 4 points wide above the
    hysteresis line, no usable resets_at — 8 of 160 swept shapes) and the fix.
    These four shapes sit outside it and released even before that fix; they are
    kept because a first pass measured only these and concluded the whole
    finding was unreproducible.

    The premise is real: `disabled-active` departing an unreadable active
    persists ``leftHeadroom: null, leftRecoveryAt: null, leftTrigger:
    "disabled-active"``, and `_left_account_recovered` keys
    `is_failover_snapshot` on ``left_trigger == "failover"`` — False here — so a
    null-baseline record is read on the ordinary legs. A review swept
    `_left_account_recovered` in isolation over 324 fleet shapes, found 84 (26%)
    where that fork changes the answer, and reported a stranded engine.

    Measured end to end through `tick()`, these shapes do not strand: the bar is
    consulted (`_left_account_recovered` runs) and returns True on each,
    including the mediocre-peer and 99%-active ones — the latter has
    ``active_h=1``, where the band is empty. Four passing shapes were never
    evidence that the finding was wrong, only that they were the wrong four.

    The reason it is right that it releases: the bar stops ping-ponging back to
    an account left for a QUOTA reason. This departure was a POLICY one, and the
    slot cannot be a candidate again until the user re-enables it — which is
    itself the signal that they want it back.
    """

    def test_a_re_enabled_account_is_reachable_again(self, harness):
        harness.switcher.set_account_disabled("1", True)
        first = harness.tick_with_usage({
            "1": None, "2": _usage(95), "3": _usage(40),
        })
        assert first is TickOutcome.SWITCHED
        assert harness.active_number() == 3
        st = harness.state()
        assert st["leftTrigger"] == "disabled-active"
        assert st["leftHeadroom"] is None, "the null baseline is the whole point"

        harness.switcher.set_account_disabled("1", False)
        harness.clock.advance(3600)          # past any cooldown
        second = harness.tick_with_usage({
            "1": _usage(5), "2": _usage(95), "3": _usage(95),
        })
        assert second is TickOutcome.SWITCHED, (
            "stranded: the no-return bar held against a slot the user "
            f"explicitly re-enabled. events={harness.kinds()}"
        )
        assert harness.active_number() == 1

    def test_control_a_FAILOVER_departure_still_obeys_its_own_legs(self, harness):
        """CONTROL. The release must be scoped to `disabled-active`; a real
        failover snapshot keeps the behaviour it already had, so this cannot be
        fixed by disabling the bar wholesale."""
        harness.tick_with_usage({"1": None, "2": _usage(40), "3": _usage(10)})
        harness.tick_with_usage({"1": None, "2": _usage(40), "3": _usage(10)})
        out = harness.tick_with_usage({"1": None, "2": _usage(40), "3": _usage(10)})
        assert out is TickOutcome.SWITCHED
        assert harness.state()["leftTrigger"] == "failover"


class TestDisabledActiveDepartureDoesNotBarTheReEnabledSlot:
    """The narrow band where the null-baseline snapshot really does strand.

    A `disabled-active` departure off an unreadable active persists
    ``leftHeadroom: null, leftRecoveryAt: null``. `_left_account_recovered`
    keys `is_failover_snapshot` on ``left_trigger == "failover"``, so this
    record runs the ORDINARY legs — which the code says are "only reached once
    a real baseline is confirmed to exist". There is none, so the headroom leg
    is skipped by its isinstance guard and the recovery leg compares against
    `inf`; the bar holds.

    It needs six things at once, which is why a first sweep of four shapes
    missed it entirely: null-baseline departure, re-enable, a proactive tick,
    ``active_h >= 7``, a peer inside a window at most 4 points wide above the
    hysteresis line, and that peer reporting a pct with NO usable ``resets_at``
    (any usable reset releases via the recovery leg instead). Measured: 8 of
    160 swept shapes fork on the bar, at
    ``active_h=7 peer=17 | 8/19 | 9/19,20,21 | 10/20,21,23``.

    Rare, but deterministic — and re-enabling a slot is the user saying they
    want it back, which is the one moment a bar against it is plainly wrong.
    """

    def test_the_re_enabled_slot_is_reachable_inside_the_band(self, harness):
        harness.switcher.set_account_disabled("1", True)
        first = harness.tick_with_usage({
            "1": None, "2": _usage(95), "3": _usage(10),
        })
        # Guard the instrument before trusting the verdict: a sweep of this
        # shape that skips it reported 160/160 "stranded" with the active never
        # having moved, which is impossible.
        assert first is TickOutcome.SWITCHED and harness.active_number() == 3, (
            "t1 instrument broken — the departure never happened"
        )
        st = harness.state()
        assert st["leftTrigger"] == "disabled-active"
        assert st["leftHeadroom"] is None and st["leftRecoveryAt"] is None

        harness.switcher.set_account_disabled("1", False)
        harness.clock.advance(10_000)
        second = harness.tick_with_usage({
            "1": _usage(80),   # peer_h = 20
            "2": _usage(99),   # h = 1, unusable
            "3": _usage(91),   # active_h = 9 -> proactive
        })
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert second is TickOutcome.SWITCHED, (
            f"barred from a slot the user re-enabled; reasons={reasons}"
        )
        assert harness.active_number() == 1

    def test_control_the_bar_still_holds_for_a_NON_reenabled_barred_slot(
        self, harness
    ):
        """CONTROL. Releasing on `disabled-active` must not release the bar
        generally — an ordinary proactive departure keeps its own legs, so this
        cannot be fixed by returning True unconditionally."""
        first = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(1), "3": _usage(99),
        })
        assert first is TickOutcome.SWITCHED
        assert harness.state()["leftTrigger"] == "proactive"
        assert harness.state()["leftHeadroom"] is not None, (
            "an ordinary departure must record a real baseline"
        )


class TestDecisionLog:
    """Why a tick switched or did not — opt-in, one writer, its own file."""

    def test_off_by_default(self, harness):
        assert harness.settings.decision_log is False
        harness.engine._emit(NoSwitchEvent(reason="cooldown", detail="held"))
        assert not (
            harness.switcher.backup_dir / "autoswitch-decisions.log"
        ).exists()

    def test_on_writes_beside_claude_swap_log_and_nowhere_else(
        self, temp_home, caplog
    ):
        h = EngineHarness(temp_home, decision_log=True)
        ev = NoSwitchEvent(reason="cooldown", detail="held")

        with caplog.at_level(logging.DEBUG):
            h.engine._emit(ev)

        line = (h.switcher.backup_dir / "autoswitch-decisions.log").read_text()
        # UTC, from `event.ts`: the file exists to be joined against the usage
        # cache, and `%(asctime)s` is naive LOCAL.
        assert line.startswith(f"{ev.ts} "), f"want {ev.ts!r}, got {line[:40]!r}"
        assert "cooldown" in line
        # No parent, so nothing reaches the root chain -- swapping the direct
        # `Logger(...)` back to `getLogger` would spill every tick into
        # claude-swap.log, and this is what says so.
        assert "cooldown" not in caplog.text

    def test_the_writer_follows_the_LIVE_lock(self, temp_home):
        # Only the holder writes, and the gate is read live: a demoted engine
        # is silent, a stopped one goes silent, and one PROMOTED on a later
        # tick starts. Through `_retry_live_promotion`, the real path -- an
        # earlier version drove the bind hook directly and survived deleting
        # its call site.
        h = EngineHarness(temp_home, decision_log=True)
        log = h.switcher.backup_dir / "autoswitch-decisions.log"
        demoted = h._make_engine(dry_run=True)

        demoted._emit(NoSwitchEvent(reason="demoted", detail=""))
        h.engine.stop()
        h.engine._emit(NoSwitchEvent(reason="stopped", detail=""))
        assert not log.exists(), "only the LIVE holder writes"

        demoted.demoted_from_live = True
        demoted._retry_live_promotion()
        assert demoted.dry_run is False, "the promotion itself did not happen"
        demoted._emit(NoSwitchEvent(reason="promoted", detail=""))

        assert "promoted" in log.read_text()


class TestABrokenPipeEndsTheLoopInsteadOfOrphaningIt:
    """`_emit` swallows everything a consumer raises, and for `--once` that is
    right: `cswap auto --once --json | head -1` must keep its 0/1/2/3 exit
    contract when the pipe closes rather than turn into a traceback.

    In LOOP mode the same swallow removes the only thing that used to stop the
    process. `head` exits after one line, Python ignores SIGPIPE, and every
    later print raises -- so the engine keeps ticking, keeps switching accounts
    and keeps holding `.auto-live.lock`, which also demotes any TUI opened
    afterwards. Nothing reaches a terminal.

    THE DISCRIMINATOR IS THE MODE, NOT THE EXCEPTION, and `_emit` cannot see
    the mode. So it records, and `run_loop` -- which `--once` never enters --
    is what stops.
    """

    @staticmethod
    def _no_waiting(engine, monkeypatch):
        """The loop's own sleep is not the subject, and waiting it out is how
        this case took two minutes and a mutant took four hundred seconds --
        the very hang it is testing for. Wake immediately; the return value is
        what the assertion reads.
        """
        monkeypatch.setattr(engine._wake, "wait", lambda _t=None: True)

    def test_a_broken_pipe_ends_the_loop(self, harness, monkeypatch):
        engine = harness.engine
        self._no_waiting(engine, monkeypatch)
        engine.on_event = lambda ev: (_ for _ in ()).throw(
            BrokenPipeError(32, "Broken pipe")
        )
        assert engine.run_loop() == 0
        assert engine._consumer_gone is True

    def test_an_epipe_oserror_is_the_same_exception(self):
        """Not a second path -- a PREMISE, and it is why the check is one
        isinstance. `OSError(EPIPE, ...)` constructs a BrokenPipeError: Python
        maps the errno to the subclass. A second clause reading `.errno` was
        dead code, and the case that "covered" it was re-measuring the first.
        """
        import errno as _errno

        assert isinstance(OSError(_errno.EPIPE, "Broken pipe"), BrokenPipeError)
        assert not isinstance(OSError(_errno.ENOSPC, "No space"), BrokenPipeError)

    def test_an_unrelated_consumer_error_does_not_end_it(self, harness):
        """THE CONTROL. Stopping on any consumer error would turn a TUI
        callback bug into a dead auto-switch engine, which is what the swallow
        exists to prevent. Asserted on the FLAG, because ending the loop here
        needs `stop()` and that would pass either way."""
        engine = harness.engine
        engine.on_event = lambda ev: (_ for _ in ()).throw(ValueError("a bug"))
        engine._emit(ConfigWarningEvent(message="x"))
        assert engine._consumer_gone is False

    def test_a_full_disk_in_a_consumer_does_not_end_it(self, harness):
        """The narrowing keys on the broken pipe, not on OSError at large."""
        import errno as _errno

        engine = harness.engine
        engine.on_event = lambda ev: (_ for _ in ()).throw(
            OSError(_errno.ENOSPC, "No space left")
        )
        engine._emit(ConfigWarningEvent(message="x"))
        assert engine._consumer_gone is False


class TestAClosedPipeDoesNotOutlastItsOwnSleep:
    """The flag is read at the TOP of the loop, so the sleep it is set in runs
    to completion first — up to `MAX_SLEEP_S` holding `.auto-live.lock`.

    Measured before the fix: the loop slept 54s after the consumer was gone,
    and a rival engine started in that window could not acquire LIVE. That is
    the harm the broken-pipe fix names in its own message, arriving one sleep
    later instead of never.
    """

    def test_the_loop_stops_within_the_tick_the_pipe_died_in(
        self, harness, monkeypatch
    ):
        engine = harness.engine
        slept: list[float] = []

        real_wait = engine._wake.wait

        def recording_wait(timeout=None):
            # FAITHFUL TO `Event.wait`: a wait that returns because the event
            # is SET is not a sleep. Replacing it outright makes `set()`
            # unobservable, and the case then fails on a fixed engine --
            # measured, it did.
            if engine._wake.is_set():
                return real_wait(0)
            slept.append(timeout or 0.0)
            return real_wait(0)

        monkeypatch.setattr(engine._wake, "wait", recording_wait)
        engine.on_event = lambda ev: (_ for _ in ()).throw(BrokenPipeError())

        assert engine.run_loop() == 0
        assert engine._consumer_gone is True
        assert slept == [] or max(slept) == 0.0, (
            f"the loop slept {max(slept):.1f}s after the consumer was gone, "
            "holding the LIVE lock for a window a rival engine is demoted in"
        )


class TestTheAtLimitEscapeDoesNotLandOnASliver:
    """Upstream issue: the escape landed on 2 points and stranded the fleet.

    At-limit skips the healthy-landing gate deliberately -- a blocked account
    is worth leaving for a working one -- so the only test a candidate faces
    is `h > 0` and the key falls through to `-h`. When NOTHING is working that
    picks whoever holds the largest sliver, which is routinely an account
    bound by its WEEKLY window with days to run, while the account being left
    was bound only by a five-hour window minutes from resetting.

    Reported tick: four accounts, threshold 95, active on 4 at its 5-hour
    limit with 40 minutes to go; peers at 0, 0 and 2 points, the 2-point one
    at 98% weekly with four days to run. The engine moved there, reported
    `all-exhausted` two minutes later, and named the active's own 40-minute
    reset as `earliestResetAt` -- it had the answer in hand and had already
    moved off it.

    `all_above` is the state where the recovery ranking exists, and it was
    scoped to the proactive triggers only. Failover stays out: there the
    active is dead or unreadable, and its reset is not a quota fact anyone can
    wait for.
    """

    def _args(self, harness, **over):
        from claude_swap.settings import AutoSwitchSettings

        now = harness.clock.now
        args = dict(
            trigger="at-limit",
            consume_first=False,
            no_return=None,
            oauth_candidates=["1", "2", "3"],
            usage={
                # The active: 5-hour window at its limit, back in 40 minutes.
                "4": _usage7(100.0, 40.0),
                "1": _usage7(100.0, 95.0),
                "2": _usage7(100.0, 100.0),
                # The sliver: 2 points, and they are on a WEEKLY window that
                # does not return for four days.
                "3": _usage7(1.0, 98.0, _iso_at(now + 4 * 86400)),
            },
            headroom={"1": 0.0, "2": 0.0, "3": 2.0, "4": 0.0},
            current="4",
            active_headroom=0.0,
            settings=AutoSwitchSettings(threshold=95.0),
            now=now,
        )
        args["usage"]["4"]["five_hour"]["resets_at"] = _iso_at(now + 40 * 60)
        args.update(over)
        return args

    def test_the_sliver_does_not_win_when_it_comes_back_last(self, harness):
        ordered, any_known, _, _ = harness.engine._rank_candidates(
            **self._args(harness)
        )
        assert any_known, "premise: the rows were unreadable, so nothing ranked"
        assert list(ordered) == [], (
            f"the escape chose {list(ordered)} — account 3 holds 2 points on a "
            "weekly window four days out, and the account being left is back "
            "in forty minutes. Landing there costs the fleet those four days"
        )

    def test_a_peer_back_sooner_than_the_active_still_wins(self, harness):
        """THE OVER-CORRECTION GUARD. Same shape, with the sliver's binding
        window resetting BEFORE the active's, so the move is the right one.

        It does NOT discriminate the fix: `-h` picks account 3 here too,
        because it is the only candidate with any headroom at all. What it
        catches is a gate that refuses every candidate once `all_above` holds
        -- which is what a fix one clause wider than this one produces, and
        the case above cannot see it."""
        now = harness.clock.now
        args = self._args(harness)
        args["usage"]["3"] = _usage7(1.0, 98.0, _iso_at(now + 5 * 60))
        ordered, _, _, _ = harness.engine._rank_candidates(**args)
        assert list(ordered) == ["3"], (
            f"got {list(ordered)} — account 3 is back in five minutes against "
            "the active's forty, which is the move the escape exists to make"
        )

    def test_no_knowable_reset_anywhere_still_lets_the_escape_escape(
        self, harness
    ):
        """`_binding_recovery_ts` answers `inf` for unknown AND for already
        past, so a fleet whose rows have gone stale makes every recovery
        `inf`. `inf >= inf - RECOVERY_HYSTERESIS_S` is True for every
        candidate, and the escape then refuses every landing FOREVER -- there
        is no state change that can clear it.

        The recovery axis needs a knowable return time for the account we are
        LEAVING. Without one there is nothing to rank against, and headroom is
        the only question left, which is what the escape did before.
        """
        args = self._args(harness)
        for u in args["usage"].values():
            for w in u.values():
                w.pop("resets_at", None)
        ordered, _, _, _ = harness.engine._rank_candidates(**args)
        assert list(ordered) == ["3"], (
            f"the escape chose {list(ordered)} — no window in the fleet says "
            "when anything comes back, so waiting for the active is waiting "
            "for a moment nothing can name"
        )

    def test_a_healthy_peer_is_still_taken_the_ordinary_way(self, harness):
        """The second control: this must change nothing when the fleet is not
        all above the threshold. A peer with real headroom wins on headroom,
        whatever its reset says."""
        now = harness.clock.now
        args = self._args(harness)
        args["usage"]["3"] = _usage7(1.0, 20.0, _iso_at(now + 4 * 86400))
        args["headroom"]["3"] = 80.0
        ordered, _, _, _ = harness.engine._rank_candidates(**args)
        assert list(ordered) == ["3"], (
            f"got {list(ordered)} — 80 points is a healthy landing and the "
            "escape must still take it"
        )


class TestTheDeliberateWaitNamesTheResetItIsWaitingFor:
    """The state the at-limit recovery ranking newly creates, and the outcome
    it did not have.

    An empty ranking from the escape means two different things now. Either
    nothing was viable -- keep the ordinary cadence, a candidate can turn
    viable at any moment -- or every peer holds a sliver that comes back LATER
    than the account we are on, and the engine has DECIDED to wait. Only the
    second has an end anybody can name.

    `truly_exhausted` cannot separate them: it asks whether every candidate is
    at zero, and the slivers are above zero by construction. So the wait was
    reported as `no-qualifying-candidate`, the reset-aware sleep never armed,
    and the engine polled its way through a window it had already measured.
    """

    def _tick(self, harness):
        now = harness.clock.now
        soon = _iso_at(now + 40 * 60)
        far = _iso_at(now + 4 * 86400)
        active = _usage7(100.0, 40.0)
        active["five_hour"]["resets_at"] = soon
        return harness.tick_with_usage({
            "1": active,                       # at its limit, back in 40 min
            "2": _usage7(1.0, 98.0, far),      # 2 points, weekly, 4 days out
            "3": _usage7(100.0, 100.0, far),   # nothing left at all
        })

    def test_the_wait_announces_the_reset_and_arms_the_sleep(self, harness):
        outcome = self._tick(harness)
        assert outcome is TickOutcome.BLOCKED
        assert harness.active_number() == 1, "premise: it moved, so it did not wait"
        exhausted = [e for e in harness.events if isinstance(e, AllExhaustedEvent)]
        assert exhausted, (
            "the engine held the machine for a reset it had measured and "
            "reported `no-qualifying-candidate` — the reason it waited is "
            f"nowhere in {[type(e).__name__ for e in harness.events]}"
        )
        assert exhausted[-1].earliest_reset_at is not None, (
            "the wait was announced without the moment it ends"
        )
        assert harness.engine._sleep_until_ts is not None, (
            "the reset-aware sleep never armed, so this polls the ordinary "
            "cadence for the whole window"
        )

    def test_a_deliberate_wait_is_not_reported_as_an_exhausted_fleet(
        self, harness
    ):
        """The wait's own gate proves the fleet is not exhausted.

        It is entered BECAUSE every candidate was read and one still holds
        quota, and then said "all accounts exhausted" to the panel, the JSON
        payload and the decision log.
        """
        outcome = self._tick(harness)
        assert outcome is TickOutcome.BLOCKED
        exhausted = [e for e in harness.events if isinstance(e, AllExhaustedEvent)]
        assert exhausted, "premise: no wait was announced, so there is nothing to judge"
        ev = exhausted[-1]
        assert ev.deliberate_wait is True, (
            "the wait was reported as an exhausted fleet, contradicting the "
            "precondition that created it"
        )
        assert "exhausted" not in ev.human(), (
            f"the human line still calls this an exhausted fleet: {ev.human()!r}"
        )

    def test_the_wait_announces_the_soonest_reset_it_can_prove(self, harness):
        """A peer that CAN prove a sooner return must not lose to the active's.

        The fallback substitutes the ACTIVE account's own recovery, which is
        the one value the gate guarantees is finite -- and is not the earliest.
        Any blocked peer with a provable reset before it is discarded, while
        `human()` renders the result as "earliest reset".
        """
        harness.seed(4, "d@example.com")
        now = harness.clock.now
        # THE PEER HOLDS THE EARLIEST, or this cannot tell the announcement
        # apart from the active's own recovery — which is the fallback the
        # docstring says must lose. It is four minutes sooner, inside
        # RECOVERY_HYSTERESIS_S, so taking the wall on the account that lifts
        # first does not fire and the tick still ends BLOCKED.
        active = _usage7(100.0, 40.0)
        active["five_hour"]["resets_at"] = _iso_at(now + 14 * 60)
        unprovable = _usage7(100.0, 100.0)
        unprovable["five_hour"]["resets_at"] = None
        unprovable["seven_day"]["resets_at"] = None
        sooner = _usage7(100.0, 40.0)
        sooner["five_hour"]["resets_at"] = _iso_at(now + 10 * 60)
        outcome = harness.tick_with_usage({
            "1": active,
            "2": _usage7(1.0, 98.0, _iso_at(now + 4 * 86400)),
            "3": unprovable,
            "4": sooner,
        })
        assert outcome is TickOutcome.BLOCKED
        exhausted = [e for e in harness.events if isinstance(e, AllExhaustedEvent)]
        assert exhausted, "premise: no wait was announced, so there is nothing to judge"
        announced = exhausted[-1].earliest_reset_at
        assert announced == _iso_at(now + 10 * 60), (
            f"announced {announced!r}, but account 4 proves it returns at "
            f"{_iso_at(now + 10 * 60)!r} -- the wait named the active's own "
            "reset over one it could prove was ninety minutes sooner"
        )

    def test_an_unprovable_peer_keeps_the_bounded_recheck(self, harness):
        """Announcing a reset is not the same as sleeping toward it.

        `_earliest_recovery`'s own contract: a blocked account whose exhausted
        windows carry no reset "could recover at any moment, so ... let the
        bounded blocked-cadence fallback re-check, rather than sleeping toward
        another account's later known reset." `_blocked_wait_long` is already
        set five lines above, so the un-armed path is NO_RESET_FALLBACK_S --
        never the ordinary cadence, which is what arming it was justified by.
        """
        now = harness.clock.now
        active = _usage7(100.0, 40.0)
        active["five_hour"]["resets_at"] = _iso_at(now + 41 * 60)
        unprovable = _usage7(100.0, 100.0)
        unprovable["five_hour"]["resets_at"] = None
        unprovable["seven_day"]["resets_at"] = None
        outcome = harness.tick_with_usage({
            "1": active,
            "2": _usage7(1.0, 98.0, _iso_at(now + 4 * 86400)),
            "3": unprovable,
        })
        assert outcome is TickOutcome.BLOCKED
        exhausted = [e for e in harness.events if isinstance(e, AllExhaustedEvent)]
        assert exhausted, "premise: no wait was announced, so there is nothing to judge"
        assert exhausted[-1].earliest_reset_at is not None, (
            "premise: nothing was announced, so announcing and sleeping cannot "
            "be told apart here"
        )
        assert harness.engine._next_delay(outcome) == NO_RESET_FALLBACK_S, (
            "the wait slept toward a reset while a peer could return at any "
            "moment -- `_earliest_recovery` refused to answer for exactly that "
            "reason, and the fallback overrode it"
        )

    def test_an_unreadable_candidate_is_not_announced_as_an_exhausted_fleet(
        self, harness
    ):
        """The state the readability half of the gate separates, and it had
        no witness at all.

        Every readable candidate is at zero, so there is nothing to land on --
        but one row could not be read, and an unreadable row is not a measured
        account. Announcing a reset here says the fleet is exhausted when one
        of its accounts may be perfectly healthy, and arms a sleep toward a
        moment nobody chose.

        `truly_exhausted` cannot cover it: it requires every candidate
        readable, and this one is not.
        """
        now = harness.clock.now
        active = _usage7(100.0, 40.0)
        active["five_hour"]["resets_at"] = _iso_at(now + 40 * 60)
        outcome = harness.tick_with_usage({
            "1": active,
            "2": _usage7(100.0, 100.0, _iso_at(now + 11 * 86400)),
            "3": "usage-unavailable",
        })
        assert outcome is TickOutcome.BLOCKED
        assert not [e for e in harness.events if isinstance(e, AllExhaustedEvent)], (
            "a fleet with an unreadable row was announced as exhausted — that "
            "row may be a healthy account, and nothing here has measured it"
        )
        assert harness.engine._sleep_until_ts is None, (
            "the sleep armed toward a reset chosen over an account nobody read"
        )

    def test_a_readable_peer_with_room_does_not_excuse_an_unread_one(
        self, harness
    ):
        """The sibling above passes for a reason unrelated to the unread row.

        Its only other candidate is exhausted, so the headroom clause is
        False whatever the unreadable row holds. Give ONE peer a sliver and
        that clause is satisfied by the peer while the row nobody read goes
        through with it -- a deliberate wait announced and a reset-aware sleep
        armed over an account this tick never measured. Only the readability
        conjunct stops it.

        The gate has to ask about the SAME accounts the comment names: every
        candidate readable, not merely one of them holding room.
        """
        now = harness.clock.now
        active = _usage7(100.0, 40.0)
        active["five_hour"]["resets_at"] = _iso_at(now + 40 * 60)
        outcome = harness.tick_with_usage({
            "1": active,
            "2": _usage7(1.0, 98.0, _iso_at(now + 11 * 86400)),
            "3": "usage-unavailable",
        })
        assert outcome is TickOutcome.BLOCKED
        assert not [e for e in harness.events if isinstance(e, AllExhaustedEvent)], (
            "a wait was announced while one candidate's usage was never read; "
            "the readable peer's sliver is what satisfied the gate"
        )
        assert harness.engine._sleep_until_ts is None, (
            "the sleep armed toward a reset chosen over an account nobody read"
        )
        assert harness.engine._next_delay(outcome) < NO_RESET_FALLBACK_S, (
            "the poll was slowed for a fleet one of whose rows is unmeasured"
        )

    def test_an_ordinary_hysteresis_block_keeps_the_ordinary_cadence(
        self, harness
    ):
        """THE CONTROL, and it has to reach the SAME arm.

        A proactive tick whose candidates are all READABLE and healthy but
        none clears the hysteresis margin: `ordered` is empty for a reason
        that can change on the next poll, so no reset may be announced and no
        sleep armed. An unreadable fleet does NOT test this -- `any_known` is
        False there and the tick returns at `no-comparison`, several arms
        earlier, so the widening this control exists to catch sails past it.
        Measured: with the wait widened to every empty ranking, the
        unreadable-fleet version passed and this one fails.
        """
        # The hysteresis margin gate this control exercises is "best"'s;
        # consume-first (now the default) admits any below-threshold peer
        # once the active is over threshold, so it must be pinned to keep
        # testing what it tests.
        harness.engine.settings = replace(harness.engine.settings, strategy="best")
        outcome = harness.tick_with_usage({
            "1": _usage(92),   # active, over the threshold -> proactive
            "2": _usage(88),   # healthy, but only 4 points better than active
            "3": _usage(88),
        })
        assert outcome is TickOutcome.BLOCKED
        assert not [e for e in harness.events if isinstance(e, AllExhaustedEvent)], (
            "an ordinary hysteresis block was announced as a wait with an end"
        )
        assert harness.engine._sleep_until_ts is None


class TestTheBindingRecoveryAgreesWithWhenTheAccountIsUsable:
    """`_binding_recovery_ts` and `_earliest_recovery` must not disagree about
    the SAME account, and on the ordinary exhausted shape they did.

    `_earliest_recovery` takes the LATEST reset among an account's >=100%
    windows, and says why: "an account blocked on both 5h and a scoped weekly
    limit isn't usable when the 5h rolls over". `_binding_recovery_ts` takes
    `max(windows, key=pct)`, and on a tie `max` returns the FIRST -- which is
    `5h`, because that is the order `relevant_windows` emits.

    So an account at 100/100 reports "back in 40 minutes" to the ranking and
    "back in four days" to the announcement, from one snapshot. The ranking
    then refuses every peer that returns inside those four days, and the
    engine says it is waiting for a reset it is not ranking against.
    """

    def test_a_tie_at_the_limit_reports_the_later_reset(self, harness):
        from claude_swap.autoswitch import _binding_recovery_ts

        now = harness.clock.now
        soon, far = _iso_at(now + 40 * 60), _iso_at(now + 4 * 86400)
        usage = {
            "five_hour": {"pct": 100.0, "resets_at": soon},
            "seven_day": {"pct": 100.0, "resets_at": far},
        }
        got = _binding_recovery_ts(usage, (), now)
        assert got == pytest.approx(now + 4 * 86400), (
            f"reported back in {(got - now) / 3600:.1f}h — both windows are at "
            "the limit, so the account is not usable until the LATER one "
            "resets, which is what _earliest_recovery already says"
        )

    def test_a_single_binding_window_is_unchanged(self, harness):
        """THE CONTROL. Only the tie moves; one clear binding window must
        still report its own reset, not the latest in the account."""
        from claude_swap.autoswitch import _binding_recovery_ts

        now = harness.clock.now
        usage = {
            "five_hour": {"pct": 100.0, "resets_at": _iso_at(now + 40 * 60)},
            "seven_day": {"pct": 40.0, "resets_at": _iso_at(now + 4 * 86400)},
        }
        assert _binding_recovery_ts(usage, (), now) == pytest.approx(now + 40 * 60)

    def test_the_two_readers_agree_when_the_blockers_are_NOT_tied(
        self, harness
    ):
        """A TIE AT 100 IS SUFFICIENT FOR AGREEMENT, NOT THE BOUNDARY OF IT.

        `_binding_recovery_ts` narrowed to the max-pct tie; the announcement
        takes EVERY window at or above 100. Those are the same subject only
        when the blockers carry an identical pct -- one ulp apart and the
        ranking says unknowable while the announcement names a moment, which
        is the crossing this function has now been corrected for twice.

        Nothing is at 100 by contract: `build_usage_result` copies
        `utilization` through with no clamp, and `account_headroom` documents
        <= 0 as "at OR OVER a limit".
        """
        now = 1_000_000.0

        def iso(dt):
            import time as _t
            return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(now + dt))

        from claude_swap.autoswitch import _binding_recovery_ts
        from claude_swap.poll_policy import limiting_reset_ts

        usage = {"five_hour": {"pct": 100.0, "resets_at": iso(2400)},
                 "seven_day": {"pct": 100.5}}
        ranked = _binding_recovery_ts(usage, (), now)
        announced = limiting_reset_ts(usage, ())
        assert ranked == announced, (
            "two windows are blocking at unequal pct: the ranking reads "
            f"{ranked!r} and the announcement {announced!r}, so the engine "
            "sorts an account as unknowable while telling the user when it "
            "comes back"
        )

    def test_the_two_readers_agree_on_a_partly_unknown_tie(self, harness):
        """THE INVARIANT THIS CLASS IS NAMED FOR, and the shape that broke it
        in the other direction.

        A first fix made a tie where ANY member names no reset answer `inf`.
        That is more conservative and it is wrong here, because
        `limiting_reset_ts` -- what `_earliest_recovery` announces from --
        SKIPS a window with no reset and answers with the latest of the ones it
        has. So the pair disagreed again, now with the ranking calling `inf`
        what the announcement called forty minutes: the same crossing this
        class exists to prevent, pointing the other way.

        Asserted as AGREEMENT rather than against a hand-written number, so it
        cannot drift the way a copied expectation does. Scoped to a tie at 100,
        which is where the two functions have the same subject at all.
        """
        from claude_swap.autoswitch import _binding_recovery_ts
        from claude_swap.poll_policy import limiting_reset_ts

        now = harness.clock.now
        usage = {
            "five_hour": {"pct": 100.0, "resets_at": _iso_at(now + 40 * 60)},
            "seven_day": {"pct": 100.0},          # blocked, and will not say for how long
        }
        assert _binding_recovery_ts(usage, (), now) == limiting_reset_ts(usage, ()), (
            "the ranking and the announcement read one account differently: "
            "the ranking sorts it last as unknowable while the announcement "
            "names a moment, so the engine waits for a reset it never ranked"
        )

    def test_a_tie_with_no_reset_at_all_is_unknown(self, harness):
        """THE CONTROL. Skipping unknown members is not the same as ignoring
        them: with NOT ONE tied window naming a reset there is nothing to take
        the latest OF, and both readers say so."""
        from claude_swap.autoswitch import _binding_recovery_ts
        from claude_swap.poll_policy import limiting_reset_ts

        now = harness.clock.now
        usage = {"five_hour": {"pct": 100.0}, "seven_day": {"pct": 100.0}}
        assert _binding_recovery_ts(usage, (), now) == float("inf")
        assert limiting_reset_ts(usage, ()) is None

    def test_the_wait_ranks_against_the_reset_it_announces(self, harness):
        """The consequence, through the engine: a peer with quota RIGHT NOW is
        refused, and the reset announced is one the ranking never used."""
        now = harness.clock.now
        active = _usage7(100.0, 100.0, _iso_at(now + 4 * 86400))
        active["five_hour"]["resets_at"] = _iso_at(now + 40 * 60)
        # PEER 2'S OWN BINDING WINDOW CARRIES A RESET. Its 5h is what binds it
        # (98 against 50), and a binding window with no `resets_at` is `inf` by
        # construction -- which would refuse it for a reason that has nothing
        # to do with the tie under test.
        peer = _usage7(98.0, 50.0, _iso_at(now + 4 * 86400))
        peer["five_hour"]["resets_at"] = _iso_at(now + 2 * 3600)
        outcome = harness.tick_with_usage({
            "1": active,
            "2": peer,                                            # 2 points, usable
            "3": _usage7(100.0, 100.0, _iso_at(now + 4 * 86400)),
        })
        assert outcome is TickOutcome.SWITCHED, (
            "the active is blocked for four days and account 2 has quota now — "
            "the escape held the machine on the dead account because the "
            "ranking read the 5-hour reset the account is not waiting for"
        )
        assert harness.active_number() == 2







class TestTheModelWindowBindsUnlessItBindsEverywhere:
    """The 2026-09-07 21:37Z incident, reproduced as fixtures: a fleet stuck
    switching ONTO, and then pinned ON, a Fable-100% wall (account 4) while a
    real Fable-82% candidate/escape (account 2) sat idle. Root cause: the
    "a model window is not a blackout" retry (`_rank_candidates`, the
    proactive/alternation ranker's own copy, and `_dynamic_active_headroom`'s
    widening) dropped the model criteria whenever ITS OWN pass came back
    empty, without ever asking whether the ACTIVE was model-walled too — so
    an active with genuine model headroom got treated as if the whole fleet
    were blacked out. `_model_window_binds_everywhere` is the one predicate
    all three now consume: false the moment ANY account (active included) is
    still open with the model folded in.

    F1/F4/F5 drive `_rank_candidates` directly (an ENGINE method, bound to a
    real `EngineHarness`'s switcher/settings — not a bare pure-function
    reimplementation), the same pattern `TestAModelWindowIsNotABlackout`
    already uses: a `dynamic`/consume-first-shaped trigger only admits a
    candidate whose weekly reset is SOONER than the active's, so every
    fixture below gives the active a far-out reset and candidates a sooner
    one — a fixture that gives every account the same reset date fails for
    that reason alone, not the one under test (measured while building
    this: a uniform `days_out` masked every case behind `reset_ts >=
    active_reset_ts`).

    F2/F3/F6 drive the full `tick()` — reachable there because an active
    genuinely AT its wall (Fable 100%, model-gated headroom exactly 0)
    classifies as `dynamic`'s `at-limit` trigger, which bypasses both the
    alternation machinery and (structurally, by `_COOLDOWN_GATED_TRIGGERS`
    excluding `at-limit`) the cooldown gate — so F2/F3 land on the SAME
    widening fix via two different entry points, and F3's cooldown state
    turns out to be inert for this exact roster (see its own docstring).
    """

    @staticmethod
    def _u(five_h, seven_d, fable, days_out):
        now = 1_000_000.0
        return {
            "five_hour": {"pct": five_h},
            "seven_day": {"pct": seven_d, "resets_at": _iso_at(now + days_out * 86400)},
            "scoped": [{"name": "Fable", "pct": fable}],
        }

    def test_f1_never_switch_into_a_wall(self, temp_home):
        """The 21:37:18Z census verbatim: active #2 (5h 15/7d 60/Fable 82,
        genuinely open on the model axis) must not have #4 (Fable 100)
        rescued into the ranking just because #4/#1/#3 are all model-walled
        — the model window does not bind everywhere while #2 is open.
        """
        cls = TestAModelWindowIsNotABlackout()
        h = EngineHarness(temp_home, model="Fable", threshold=90.0)
        usage = {
            "2": self._u(15, 60, 82, 100),
            "4": self._u(8, 86, 100, 2),
            "1": self._u(0, 100, 92, 4),
            "3": self._u(0, 79, 100, 3),
        }
        headroom = {n: oauth.account_headroom(v, ("Fable",)) for n, v in usage.items()}
        args = cls._args(
            h, usage=usage, current="2", oauth_candidates=["1", "3", "4"],
            headroom=headroom, active_headroom=headroom["2"],
        )
        ordered, _, _, _ = h.engine._rank_candidates(**args)
        assert list(ordered) == [], (
            f"got {list(ordered)!r} — account 2 is open on the model axis "
            "(Fable 82% < the 90% threshold), so the window does not bind "
            "everywhere and the retry must not rescue #4's model-only wall"
        )

    def test_f2_leave_a_wall(self, temp_home):
        """Active #4 fully walled on Fable (100%, model-gated headroom 0,
        5h/7d otherwise fine); candidate #2 open (Fable 82%). The owner's
        stated expectation: switch OUT of the wall to the best real-headroom
        candidate. Pre-fix this held at #4 forever (`_dynamic_active_
        headroom` widened 0 to the unmodeled 14, so `_classify_dynamic_
        trigger` never saw the wall)."""
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num, email in ((4, "a4@example.invalid"), (2, "a2@example.invalid")):
            h.seed(num, email)
        h.make_live("a4@example.invalid", 4)
        fleet = {"4": self._u(8, 86, 100, 1), "2": self._u(15, 60, 82, 1)}
        outcome = h.tick_with_usage(fleet)
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome!r} — a Fable-100% active with a Fable-82% "
            "candidate open must switch, not hold"
        )
        assert h.active_number() == 2

    def test_f4_control_the_primary_pass_still_lands_it(self, temp_home):
        """F1's roster with #4's Fable at 50% instead of 100%: #4's own
        binding window is now 7d (86%, headroom 14), never blocked at the
        90% threshold — admitted by the PRIMARY (model-gated) pass with no
        retry involved at all. Identical before and after this round's
        fix: the predicate only gates the RETRY, never the primary pass.
        """
        cls = TestAModelWindowIsNotABlackout()
        h = EngineHarness(temp_home, model="Fable", threshold=90.0)
        usage = {
            "2": self._u(15, 60, 82, 100),
            "4": self._u(8, 86, 50, 2),
            "1": self._u(0, 100, 92, 4),
            "3": self._u(0, 79, 100, 3),
        }
        headroom = {n: oauth.account_headroom(v, ("Fable",)) for n, v in usage.items()}
        args = cls._args(
            h, usage=usage, current="2", oauth_candidates=["1", "3", "4"],
            headroom=headroom, active_headroom=headroom["2"],
        )
        ordered, _, _, _ = h.engine._rank_candidates(**args)
        assert list(ordered) == ["4"], (
            f"got {list(ordered)!r} — #4 is not model-blocked (Fable 50%) "
            "and must land via the primary pass alone"
        )

    def test_f5_control_a_true_fleet_wide_blackout_still_retries(self, temp_home):
        """The design intent #321 exists for, preserved: active #4 AND
        every candidate (#1, #3) are Fable-walled (100%) — a genuine
        fleet-wide model blackout. The retry must still drop the model set
        and rank on 5h/7d: #1's 7d (92%) still blocks it there, #3's (79%)
        does not, so #3 is the only admissible landing.
        """
        cls = TestAModelWindowIsNotABlackout()
        h = EngineHarness(temp_home, model="Fable", threshold=90.0)
        usage = {
            "4": self._u(8, 86, 100, 100),
            "1": self._u(0, 92, 100, 4),
            "3": self._u(0, 79, 100, 3),
        }
        headroom = {n: oauth.account_headroom(v, ("Fable",)) for n, v in usage.items()}
        args = cls._args(
            h, usage=usage, current="4", oauth_candidates=["1", "3"],
            headroom=headroom, active_headroom=headroom["4"],
        )
        ordered, _, _, _ = h.engine._rank_candidates(**args)
        assert list(ordered) == ["3"], (
            f"got {list(ordered)!r} — a true fleet-wide model blackout "
            "must still retry on 5h/7d and rank #3 (7d 79%), never come "
            "back empty just because this round bounds the retry"
        )

    def test_f6_no_flap_back_onto_a_still_walled_account(self, temp_home):
        """After F2's escape (4 -> 2), the next tick — same fleet, #4 still
        Fable-walled, #2 still open — must not flap back to #4. Emerges
        for free from the fix: #2 (active) is open, so `_model_window_
        binds_everywhere` is false and #4's model-only wall is never
        dropped back into the ranking.
        """
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num, email in ((4, "a4@example.invalid"), (2, "a2@example.invalid")):
            h.seed(num, email)
        h.make_live("a4@example.invalid", 4)
        fleet = {"4": self._u(8, 86, 100, 1), "2": self._u(15, 60, 82, 1)}
        outcome1 = h.tick_with_usage(fleet)
        assert outcome1 is TickOutcome.SWITCHED
        assert h.active_number() == 2
        h.clock.advance(301.0)
        h.events.clear()
        outcome2 = h.tick_with_usage(fleet)
        assert outcome2 is not TickOutcome.SWITCHED, (
            f"got {outcome2!r} — must not flap back onto #4 while it is "
            "still Fable-walled"
        )
        assert h.active_number() == 2

    def test_f7_dynamic_healthy_arm_retries_a_true_fleet_wide_blackout(
        self, temp_home
    ):
        """F5's fixture (active #4 AND every candidate Fable-walled, a
        genuine fleet-wide model blackout) driven through the FULL
        `tick()`, not `_rank_candidates` directly: `_dynamic_active_
        headroom` widens the active's headroom to its unmodeled 14 (5h
        8%/7d 86%), so `_classify_dynamic_trigger` reads it as HEALTHY
        (`dynamic-healthy`, not `at-limit`) and the tick falls into the
        alternation arm instead of `_rank_candidates`'s own retry — which
        F5 exercises directly and so never catches this. That arm ranked
        candidates on the still-model-gated `headroom` dict with no retry
        of its own, so every real candidate (also Fable-walled) read as
        spent and the tick held below-threshold forever instead of
        dropping the model set and switching to #3 (7d 79%, open once
        Fable drops), exactly as `_rank_candidates`'s own retry does.

        NO account is seeded warm: a genuine fleet-wide blackout must not
        presuppose a WARM partner (the alternation arm's ordinary partner
        search only ever considers `warm_ordered`) — the walled-escape
        must admit a COLD candidate once the model set has legitimately
        been dropped fleet-wide, ranked on the unmodeled floor headroom
        (#1 stays below `cold_switch_cost_pct` at 8; #3 clears it at 21).
        """
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num, email in (
            (4, "a4@example.invalid"),
            (1, "a1@example.invalid"),
            (3, "a3@example.invalid"),
        ):
            h.seed(num, email)
        h.make_live("a4@example.invalid", 4)
        fleet = {
            "4": self._u(8, 86, 100, 100),
            "1": self._u(0, 92, 100, 4),
            "3": self._u(0, 79, 100, 3),
        }
        outcome = h.tick_with_usage(fleet)
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome!r} — a true fleet-wide Fable blackout must "
            "still retry on 5h/7d and switch to #3, not hold "
            "below-threshold forever because the active's widened "
            "headroom reads healthy, and it must not require a warm "
            "partner to do it"
        )
        assert h.active_number() == 3





class TestBoundedWalledEscapeUnderDynamicHealthy:
    """#403 (owner, 2026-09-08): the owner's live specimen — a Fable-
    walled active whose OWN 5h/7d widen it to `dynamic-healthy` (#375's
    widening is untouched, and correctly reads it that way: this active's
    5h/7d genuinely have room) — but every real peer is ALSO blocked on
    some axis, so #375's alternation arm (which only ever considers a WARM
    partner) held forever with real quota sitting idle. `dynamic-healthy`
    gains a bounded escape: when no warm partner qualifies AND the active
    is genuinely walled on its own (never-widened) model-gated axis, land
    on whichever admissible peer (real headroom, no fixed percentage bar)
    recovers soonest — cold or warm, ranked by time-to-reset, warmth
    breaking only an exact tie. Never reachable while the active has real
    room (the owner-fixture tests in ``TestTheModelWindowBindsUnlessIt
    BindsEverywhere`` and ``TestWarmthAndAlternation375`` pin exactly that
    boundary and must stay green).
    """

    @staticmethod
    def _u(five_h, seven_d, fable, days_out=3, five_h_resets=None, fable_resets=None):
        now = 1_000_000.0
        d = {
            "five_hour": {"pct": five_h},
            "seven_day": {"pct": seven_d, "resets_at": _iso_at(now + days_out * 86400)},
            "scoped": [{"name": "Fable", "pct": fable}],
        }
        if five_h_resets is not None:
            d["five_hour"]["resets_at"] = _iso_at(now + five_h_resets)
        if fable_resets is not None:
            d["scoped"][0]["resets_at"] = _iso_at(now + fable_resets)
        return d

    def test_the_owners_specimen_switches_to_7(self, temp_home):
        """The reported fleet, exactly as measured: active #4 walled on
        Fable (100%) and effectively spent on 7d (90%) — #375's widen
        reads it `dynamic-healthy` off its real 18% 5h room, correctly.
        Every real peer is ALSO blocked on some axis (#3/#2 on Fable,
        #7 on its own 5h, #1/#5/#6 on 7d) so no WARM partner exists and
        the fleet held on a dead account. #7's 5h resets in under 3
        hours; #2's Fable (its own binding window) has no known reset at
        all in this fixture — #7 must win either way, and first-tick
        (this harness has no prior `autoswitch_state.json`) must still
        decide, not hold pending a rate sample that does not exist yet.
        """
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num, email in (
            (4, "a4@example.invalid"), (3, "a3@example.invalid"),
            (2, "a2@example.invalid"), (7, "a7@example.invalid"),
            (1, "a1@example.invalid"), (6, "a6@example.invalid"),
            (5, "a5@example.invalid"),
        ):
            h.seed(num, email)
        h.make_live("a4@example.invalid", 4)
        assert not h.state(), "must decide on the very first tick, no prior state"
        fleet = {
            "4": self._u(18, 90, 100, days_out=4),
            "3": self._u(10, 81, 100, days_out=3),
            "2": self._u(57, 68, 90, days_out=2),
            "7": self._u(91, 18, 10, days_out=5, five_h_resets=2 * 3600 + 41 * 60),
            "1": self._u(0, 95, 0, days_out=4),
            "6": self._u(0, 96, 0, days_out=4),
            "5": self._u(0, 97, 0, days_out=4),
        }
        outcome = h.tick_with_usage(fleet)
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome!r} — a Fable-walled active with every real peer "
            "also blocked must escape, not hold on a dead account"
        )
        assert h.active_number() == 7, (
            f"landed on {h.active_number()} instead of #7 — the peer whose "
            "own binding window (5h) resets soonest"
        )

    def test_time_to_reset_caps_time_to_exhaustion_small_headroom_soon_wins(
        self, temp_home
    ):
        """Isolated pair, both walled on some axis (so neither is simply
        'more healthy' than the other by the ordinary tiering): SOON has
        LESS headroom (5) but its binding 5h resets in an hour; FAR has
        MORE headroom (10) but its binding Fable window resets six days
        out. SOON must win — a window that recycles soon is not a wall
        worth avoiding, however little room it leaves right now.
        """
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num, email in (
            (1, "active@example.invalid"),
            (2, "soon@example.invalid"),
            (3, "far@example.invalid"),
        ):
            h.seed(num, email)
        h.make_live("active@example.invalid", 1)
        fleet = {
            "1": self._u(18, 90, 100, days_out=4),  # active, walled on Fable
            "2": self._u(95, 10, 5, days_out=4, five_h_resets=3600),   # SOON: headroom 5
            "3": self._u(5, 10, 90, days_out=4, fable_resets=6 * 86400),  # FAR: headroom 10
        }
        outcome = h.tick_with_usage(fleet)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2, (
            f"landed on {h.active_number()} — the sooner-resetting, "
            "smaller-headroom candidate (#2) must beat the larger-"
            "headroom candidate whose own binding window is days out (#3)"
        )

    def test_warmth_breaks_an_exact_tie_between_admissible_escapees(self, temp_home):
        """Two escapees with the IDENTICAL binding reset — warmth (never
        touched) picks the warm one over the cold one."""
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num, email in (
            (1, "active@example.invalid"),
            (2, "cold@example.invalid"),
            (3, "warm@example.invalid"),
        ):
            h.seed(num, email)
        h.make_live("active@example.invalid", 1)
        h.engine._mutate_state(
            lambda s: s.update(lastActiveAt={"3": h.clock.now - 100.0})
        )
        fleet = {
            "1": self._u(18, 90, 100, days_out=4),
            "2": self._u(10, 10, 95, days_out=4, fable_resets=3600),  # cold, model-walled
            "3": self._u(10, 10, 95, days_out=4, fable_resets=3600),  # warm, same reset
        }
        outcome = h.tick_with_usage(fleet)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3, (
            f"landed on {h.active_number()} — an exact tie on binding "
            "reset must go to the warm candidate (#3), never the cold "
            "one (#2)"
        )

    def test_warmth_never_overrides_a_strictly_sooner_cold_candidate(
        self, temp_home
    ):
        """Warmth is a tie-break, never a veto in the OTHER direction
        either: a warm candidate whose own reset is later must still lose
        to a cold candidate that comes back sooner."""
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num, email in (
            (1, "active@example.invalid"),
            (2, "cold_soon@example.invalid"),
            (3, "warm_later@example.invalid"),
        ):
            h.seed(num, email)
        h.make_live("active@example.invalid", 1)
        h.engine._mutate_state(
            lambda s: s.update(lastActiveAt={"3": h.clock.now - 100.0})
        )
        fleet = {
            "1": self._u(18, 90, 100, days_out=4),
            "2": self._u(95, 10, 5, days_out=4, five_h_resets=3600),       # cold, soon
            "3": self._u(5, 10, 90, days_out=4, fable_resets=6 * 86400),  # warm, far
        }
        outcome = h.tick_with_usage(fleet)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2, (
            f"landed on {h.active_number()} — warmth must not override a "
            "candidate that genuinely recovers sooner (#2)"
        )

    def test_the_escape_never_fires_while_the_active_has_real_room(self, temp_home):
        """The active's OWN (never-widened) model-gated headroom is real
        (Fable at 50%, not 100%) — not walled — so the escape must not
        engage even though no warm partner exists and every peer is
        blocked on some axis. Mirrors the pinned owner-fixture holds in
        ``TestWarmthAndAlternation375``/``TestTheModelWindowBindsUnless
        ItBindsEverywhere``, scoped to this class so a regression here is
        caught beside the new arm it could otherwise silently override.
        """
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num, email in (
            (1, "active@example.invalid"), (2, "a@example.invalid"),
        ):
            h.seed(num, email)
        h.make_live("active@example.invalid", 1)
        fleet = {
            "1": self._u(18, 20, 50, days_out=4),   # real Fable room -- not walled
            "2": self._u(95, 10, 5, days_out=4, five_h_resets=3600),  # blocked on 5h
        }
        outcome = h.tick_with_usage(fleet)
        assert outcome is TickOutcome.NO_ACTION, (
            f"got {outcome!r} — the active has real headroom on every "
            "axis; the walled-escape must never fire for a merely-cold "
            "fleet"
        )
        assert h.active_number() == 1

    def test_an_untrustworthy_top_escapee_does_not_strand_the_tick(
        self, temp_home
    ):
        """I1: the escape handed the freshen loop a SINGLE candidate
        (`dynamic_ordered = [walled_escape]`), so a struck top escapee
        stranded the whole tick instead of falling through to the next
        admissible one — exactly the defect the `proactive` arm does not
        have, because its own `dynamic_ordered` is the full ranked list
        (`warm_ordered + [floor-clearing cold]`). Owner-specimen fixture
        (``test_the_owners_specimen_switches_to_7``), #7 (soonest binding
        reset) struck this time — #1 (next by recovery time) must still
        be reached instead of the tick stranding BLOCKED.
        """
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num, email in (
            (4, "a4@example.invalid"), (3, "a3@example.invalid"),
            (2, "a2@example.invalid"), (7, "a7@example.invalid"),
            (1, "a1@example.invalid"), (6, "a6@example.invalid"),
            (5, "a5@example.invalid"),
        ):
            h.seed(num, email)
        h.make_live("a4@example.invalid", 4)
        fleet = {
            "4": self._u(18, 90, 100, days_out=4),
            "3": self._u(10, 81, 100, days_out=3),
            "2": self._u(57, 68, 90, days_out=2),
            "7": self._u(91, 18, 10, days_out=5, five_h_resets=2 * 3600 + 41 * 60),
            "1": self._u(0, 95, 0, days_out=4),
            "6": self._u(0, 96, 0, days_out=4),
            "5": self._u(0, 97, 0, days_out=4),
        }
        entries = {num: _entry_for(value, h.clock.now) for num, value in fleet.items()}
        entries["7"] = UsageEntry(
            last_good=fleet["7"], fetched_at=h.clock.now, age_s=0.0,
            auth_dead_strikes=2,
        )
        assert entries["7"].token_dead()
        outcome = h.tick_with_entries(entries)
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome!r} — a struck top escapee (#7) must not "
            "strand the tick while a healthy, later-ranked one is "
            "available"
        )
        assert h.active_number() == 1, (
            f"landed on {h.active_number()} — with #7 struck, the next "
            "admissible escapee by recovery time must be reached"
        )

    def test_a_relative_margin_above_the_cold_floor_still_governs_admission(
        self, temp_home
    ):
        """[I2]: this test (formerly ``test_an_absolute_floor_never_holds_
        a_real_rescue_forever``) pinned its subject with a candidate at
        unmodeled 60 -- nowhere near the absolute `cold_switch_cost_pct`
        (20) floor either bar could produce, so it passed unchanged
        whether or not `_blackout_retry_admission_bar`'s RELATIVE term
        (`floor_headroom[current] + SPENT_HEADROOM_PCT`) was even applied
        to a cold candidate at all; it could no longer fail for its
        stated subject once `cold_floor` shipped. `max(relative,
        cold_floor)`'s relative half only ever BINDS (exceeds the
        absolute 20) when the active's own unmodeled headroom is in
        `[17, 20)` -- below 17 the absolute 20 wins outright, at or above
        20 `blackout_escape` itself disarms. Active at 18 puts the bar at
        21 (relative, strictly above the absolute floor): #3 at 20.5
        clears the absolute floor alone but not the actual (relative)
        bar, and must still be refused -- proving the relative term, not
        just the absolute 20, decides admission in this band. (A genuine
        cold rescue clearing BOTH bars stays pinned by
        ``test_f7_dynamic_healthy_arm_retries_a_true_fleet_wide_
        blackout``.)
        """
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num, email in ((1, "a1@example.invalid"), (3, "a3@example.invalid")):
            h.seed(num, email)
        h.make_live("a1@example.invalid", 1)
        fleet = {
            "1": self._u(0, 82, 100, days_out=4),    # active: unmodeled headroom 18
            "3": self._u(0, 79.5, 100, days_out=3),  # cold: unmodeled headroom 20.5
        }
        outcome = h.tick_with_usage(fleet)
        assert outcome is not TickOutcome.SWITCHED, (
            f"got {outcome!r} — #3's unmodeled headroom (20.5) clears "
            "the absolute cold floor (20) but not the relative bar "
            "(active 18 + SPENT_HEADROOM_PCT = 21); the relative term "
            "must still govern admission in this band"
        )
        assert h.active_number() == 1

    def test_a_peer_barely_above_the_active_is_still_not_an_escape(
        self, temp_home
    ):
        """The churn case the absolute bar used to protect against, still
        protected once the bar is relative: a candidate merely level with,
        or barely above, the active's own unmodeled reading is not a real
        rescue and must not be taken — only a margin of at least
        `SPENT_HEADROOM_PCT` counts. Both readings sit ABOVE the old
        absolute `cold_switch_cost_pct` (20) floor (18 and 20) so a naive
        revert to that absolute bar would wrongly ADMIT this candidate —
        the discriminating fixture against a plain revert.
        """
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num, email in ((1, "a1@example.invalid"), (3, "a3@example.invalid")):
            h.seed(num, email)
        h.make_live("a1@example.invalid", 1)
        fleet = {
            "1": self._u(0, 82, 100, days_out=4),  # active: unmodeled headroom 18
            "3": self._u(0, 80, 100, days_out=3),  # unmodeled headroom 20 -- barely above
        }
        outcome = h.tick_with_usage(fleet)
        assert outcome is not TickOutcome.SWITCHED, (
            f"got {outcome!r} — #3's unmodeled headroom (20) is only 2 "
            "points above the active's own (18), well under the "
            "SPENT_HEADROOM_PCT churn margin; it must not be taken"
        )
        assert h.active_number() == 1

    def test_a_warm_rescue_is_not_invisible_to_the_blackout_escape(
        self, temp_home
    ):
        """[C]: the blackout escape only ever drew candidates from
        `cold_ordered`, so a WARM peer whose unmodeled headroom clears the
        relative bar but sits under the absolute `cold_switch_cost_pct`
        (20) was invisible to it -- `partner` (only sees warm at >= 20)
        refuses it too, and the tick held `below-threshold`/NO_ACTION even
        though #3 holds more than double the active's own unmodeled
        headroom, warm, right now. Held up to an hour, and the engine
        would then take #3 the moment it goes COLD -- refusing the cheap
        rescue and buying the expensive one.
        """
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num, email in ((1, "a1@example.invalid"), (3, "a3@example.invalid")):
            h.seed(num, email)
        h.make_live("a1@example.invalid", 1)
        h.engine._mutate_state(
            lambda s: s.update(lastActiveAt={"3": h.clock.now - 600.0})
        )
        fleet = {
            "1": self._u(0, 95, 100, days_out=4),  # active: unmodeled headroom 5
            "3": self._u(0, 88, 100, days_out=3),  # warm, unmodeled headroom 12
        }
        outcome = h.tick_with_usage(fleet)
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome!r} — #3 is a WARM rescue holding more than "
            "double the active's own unmodeled headroom; the escape must "
            "not be blind to a warm candidate merely because it sits "
            "under the absolute cold-switch bar"
        )
        assert h.active_number() == 3

    def test_a_warm_partner_does_not_veto_an_immediate_walled_escape(
        self, temp_home
    ):
        """[I]: `if partner is None and _about_to_wall(...)` skipped the
        walled escape entirely whenever ANY warm peer cleared the ordinary
        `cold_switch_cost_pct` (20) bar -- even when the active is
        genuinely walled RIGHT NOW. Control then fell to the plain
        partner/dwell path, which held for a full `alternation_chunk_
        seconds` (600s) before taking the very candidate the escape would
        have picked immediately. A partner's existence is a ranking fact,
        never a veto on an urgent escape.
        """
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num, email in ((1, "b@example.invalid"), (2, "a@example.invalid")):
            h.seed(num, email)
        h.make_live("b@example.invalid", 1)
        t0 = h.clock.now
        h.engine._mutate_state(
            lambda s: s.update(lastActiveAt={"1": t0, "2": t0})
        )
        h.clock.advance(60.0)
        fleet = {
            "1": self._u(0, 95, 100, days_out=4),  # B, active: unmodeled headroom 5
            "2": self._u(0, 40, 100, days_out=4),  # A, warm: unmodeled headroom 60
        }
        outcome = h.tick_with_usage(fleet)
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome!r} — the active is walled right now (raw "
            "headroom 0) and an admissible warm partner exists; the "
            "escape must fire immediately, not wait out a 600s dwell"
        )
        assert h.active_number() == 2

    def test_the_blackout_bar_never_drops_below_the_real_switch_cost(
        self, temp_home
    ):
        """[I]: `_blackout_retry_admission_bar` was `floor_headroom[current]
        + SPENT_HEADROOM_PCT` with no floor of its own -- so as the
        active's own unmodeled reading gets smaller, the bar for a COLD
        candidate (which genuinely costs ~19 5h-points to land on,
        settings.py's own `cold_switch_cost_pct`) shrinks with it. Active
        at unmodeled 5, #3 COLD at unmodeled 15 (7d 85%, still below the
        90% threshold so it reads model-only-walled, not exhausted)
        cleared the old relative-only bar (8) for a nominal gain of 10
        against a real cold-switch cost of ~19 -- a net loss the escape
        must refuse.
        """
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num, email in ((1, "a1@example.invalid"), (3, "a3@example.invalid")):
            h.seed(num, email)
        h.make_live("a1@example.invalid", 1)
        fleet = {
            "1": self._u(0, 95, 100, days_out=4),  # active: unmodeled headroom 5
            "3": self._u(0, 85, 100, days_out=3),  # cold: unmodeled headroom 15
        }
        outcome = h.tick_with_usage(fleet)
        assert outcome is not TickOutcome.SWITCHED, (
            f"got {outcome!r} — #3's unmodeled headroom (15) clears the "
            "old relative-only bar (8) but not the real cold-switch cost "
            "(~19-worth, `cold_switch_cost_pct`); a cold escapee must "
            "clear both"
        )
        assert h.active_number() == 1

    def test_the_non_blackout_escape_still_prices_a_cold_landing(
        self, temp_home
    ):
        """[I1]: the `_about_to_wall(raw_active_headroom)`-alone gate
        (#321's collapse) also widened THIS branch's reach -- the plain
        `_about_to_wall`/`SPENT_HEADROOM_PCT` escape that fires when
        `blackout_escape` is False (no genuine fleet-wide model blackout,
        `model_window_dropped` stays False because the model-gated ranking
        already has a warm candidate). That branch has no cold cost term
        at all, so a cold candidate whose binding window merely resets
        SOONER can be taken over a qualified warm partner, paying the
        real `cold_switch_cost_pct` rewrite for nothing. Active #1 is
        walled on Fable right now (model-gated headroom 0); #2 is a warm
        partner at model-gated headroom 25; #3 is a COLD peer at
        model-gated headroom 15 -- below `cold_switch_cost_pct` (20) --
        whose own Fable window happens to reset in an hour, while #2's has
        no known reset at all. The old code sorted purely on recovery
        time and landed on #3; a cold escapee must clear the cold-switch
        floor too, same as the blackout escape already requires.
        """
        h = EngineHarness(temp_home, model="Fable", threshold=70.0, strategy="dynamic")
        for num, email in (
            (1, "a1@example.invalid"), (2, "a2@example.invalid"),
            (3, "a3@example.invalid"),
        ):
            h.seed(num, email)
        h.make_live("a1@example.invalid", 1)
        h.engine._mutate_state(
            lambda s: s.update(lastActiveAt={"2": h.clock.now - 600.0})
        )
        fleet = {
            "1": self._u(30, 40, 100, days_out=4),  # active: model-gated headroom 0
            "2": self._u(20, 25, 75, days_out=4),   # warm: model-gated headroom 25
            "3": self._u(5, 10, 85, days_out=3, fable_resets=3600),  # cold: headroom 15
        }
        outcome = h.tick_with_usage(fleet)
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome!r} — a walled active with a qualified warm "
            "partner must escape"
        )
        assert h.active_number() == 2, (
            f"landed on {h.active_number()} — #3's headroom (15) is below "
            "the real cold-switch cost (20) and must not be taken over "
            "the qualified warm partner (#2) merely because it resets "
            "sooner"
        )


class TestASpendOnlyAccountNeverDisarmsTheBlackoutPredicate:
    """The 2026-09-08 01:45Z specimen (owner): active #2 reads 97% used
    (headroom 3, `proactive` trigger). Every real peer is ALSO blocked —
    #1/#4/#5/#6/#7 on 5h/7d itself, #3 on Fable alone (5h 43%/7d 88%, real
    unmodeled headroom 12) — and #8 is spend-only (no 5h/7d/model window at
    all). `_model_window_binds_everywhere` folded #8's empty window list
    into the SAME "open" outcome a genuinely-open account produces
    (`classify_candidate_block([], threshold)` always returns "open"), so
    one unreadable/spend-only account in the fleet disarmed the retry for
    every genuinely model-only-walled account, `all accounts exhausted`
    fired with Opus (the 5h/7d axis) open on #3 the whole time, and the
    engine slept ten minutes instead of landing there.
    """

    @staticmethod
    def _u(five_h, seven_d, fable, days_out=3):
        now = 1_000_000.0
        return {
            "five_hour": {"pct": five_h},
            "seven_day": {"pct": seven_d, "resets_at": _iso_at(now + days_out * 86400)},
            "scoped": [{"name": "Fable", "pct": fable}],
        }

    @staticmethod
    def _spend_only():
        # A credit (pay-as-you-go) slot: no five_hour/seven_day/scoped keys
        # at all, exactly the shape `oauth.relevant_windows` reads as "no
        # window to report" (`test_a_spend_only_account_prints_its_credit_
        # figure_not_a_bare_mark` pins the same shape at the panel layer).
        return {"spend": {"pct": 10.0, "used": 1.0, "limit": 10.0}}

    def _specimen_usage(self, *, account_3_seven_day=88):
        return {
            "2": self._u(97, 50, 50, days_out=3),  # active: headroom 3, proactive
            "1": self._u(0, 100, 92, days_out=4),
            "3": self._u(43, account_3_seven_day, 100, days_out=2),
            "4": self._u(34, 93, 100, days_out=4),
            "5": self._u(0, 100, 100, days_out=4),
            "6": self._u(0, 100, 90, days_out=4),
            "7": self._u(100, 20, 10, days_out=5),
            "8": self._spend_only(),
        }

    def test_the_owners_specimen_switches_to_3(self, temp_home):
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num, email in (
            (2, "a2@example.invalid"), (1, "a1@example.invalid"),
            (3, "a3@example.invalid"), (4, "a4@example.invalid"),
            (5, "a5@example.invalid"), (6, "a6@example.invalid"),
            (7, "a7@example.invalid"), (8, "a8@example.invalid"),
        ):
            h.seed(num, email)
        h.make_live("a2@example.invalid", 2)
        fleet = self._specimen_usage()
        outcome = h.tick_with_usage(fleet)
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome!r} — #3 is open on the unmodeled (5h/7d) axis "
            "(12% headroom) and the spend-only #8 must not disarm the "
            "fleet-wide-blackout retry that would rescue it; events="
            f"{[e.kind for e in h.events]}"
        )
        assert h.active_number() == 3, (
            f"landed on {h.active_number()} instead of #3 — the only "
            "account with real 5h/7d headroom once the Fable wall is "
            "correctly dropped fleet-wide"
        )

    def test_a_spend_only_account_cannot_disarm_the_predicate(self, temp_home):
        """The predicate, pinned on its own: with #8 present in the fleet
        it must read exactly as it would with #8 removed — an unreadable
        or spend-only account carries no window to be "open" on."""
        from claude_swap.autoswitch import _model_window_binds_everywhere

        usage = self._specimen_usage()
        with_8 = _model_window_binds_everywhere(usage, ("Fable",), 90.0)
        without_8 = _model_window_binds_everywhere(
            {k: v for k, v in usage.items() if k != "8"}, ("Fable",), 90.0
        )
        assert with_8 is True, (
            "a spend-only account (#8) must not disarm the predicate — "
            f"got {with_8!r} with #8 present, {without_8!r} without it"
        )
        assert without_8 is True

    def test_control_no_model_only_wall_left_reads_false_and_invents_nothing(
        self, temp_home
    ):
        """Same roster, but #3's 7d is bumped to 100 too — no account is
        model-only-walled any more (every real account is "full", #8 is
        spend-only). The predicate must correctly read False, and the
        engine must not manufacture a landing that was never there."""
        from claude_swap.autoswitch import _model_window_binds_everywhere

        usage = self._specimen_usage(account_3_seven_day=100)
        assert _model_window_binds_everywhere(usage, ("Fable",), 90.0) is False, (
            "no account in this roster is model-only-walled once #3's 7d "
            "is spent too — the predicate must not fire"
        )
        # #8 is skipped via a plain `continue`, never a `return` -- it
        # costs the loop nothing about the rest of the roster, before or
        # after it in iteration order. This assertion does not discriminate
        # the fix from the pre-fix predicate: with no genuine model-only
        # wall left in the roster, BOTH read False whether #8 is present or
        # not, since #8 sits last in this fixture's iteration order and
        # nothing earlier in the roster sets the verdict either way. It
        # only pins that dropping #8 from an already-open roster is a
        # no-op. The discriminating case -- #8 sitting where its old
        # "read as open" behaviour would have masked a REAL model-only
        # wall -- is this class's `test_a_spend_only_account_cannot_
        # disarm_the_predicate`'s `with_8 is True` assertion.
        without_8 = {k: v for k, v in usage.items() if k != "8"}
        assert _model_window_binds_everywhere(without_8, ("Fable",), 90.0) is False, (
            "dropping #8 from an already-open roster must not change the "
            "verdict"
        )
        h = EngineHarness(temp_home, model="Fable", threshold=90.0, strategy="dynamic")
        for num, email in (
            (2, "a2@example.invalid"), (1, "a1@example.invalid"),
            (3, "a3@example.invalid"), (4, "a4@example.invalid"),
            (5, "a5@example.invalid"), (6, "a6@example.invalid"),
            (7, "a7@example.invalid"), (8, "a8@example.invalid"),
        ):
            h.seed(num, email)
        h.make_live("a2@example.invalid", 2)
        outcome = h.tick_with_usage(usage)
        assert outcome is not TickOutcome.SWITCHED, (
            f"got {outcome!r} — with no genuine model-only wall left in "
            "the roster the retry must not fire and the engine must not "
            "invent a candidate"
        )


class TestDynamicNeverWalls0010:
    """adr/0010: `dynamic` must never wall while any account can serve.

    R1 the cooldown yields once the active is at or under the spent bar,
    R2 the sleep is bounded while the active is within two margins of that
    bar. Both gated at ``strategy == "dynamic"``;
    ``tests/test_dynamic_isolation.py`` holds the fence for the other two
    strategies.
    """

    def _harness(self, temp_home, **kwargs):
        kwargs.setdefault("strategy", "dynamic")
        kwargs.setdefault("interval_seconds", 360.0)
        h = EngineHarness(temp_home, **kwargs)
        h.seed(1, "acct1@example.invalid")
        h.seed(2, "acct2@example.invalid")
        h.seed(3, "acct3@example.invalid")
        h.make_live("acct1@example.invalid", 1)
        return h

    # -- R1: never hold where you would not land ------------------------

    def test_r1_the_bypass_leaves_a_spent_active_and_cannot_two_cycle(
        self, temp_home
    ):
        """Tick 1: h=2 is below the bar `_rank_dynamic_candidates` refuses
        to land on, so the account cannot serve — one second into a 300s
        cooldown the engine must still leave it.

        Tick 2 is the anti-flap argument, not a scenario: landing needs
        `h > SPENT_HEADROOM_PCT` and this departure needs
        `h <= SPENT_HEADROOM_PCT`, so the account just left can never be
        the one next landed on. Still well inside the same cooldown
        window, where a bypassed tick is free to re-decide.
        """
        h = self._harness(temp_home)
        h.engine._mutate_state(lambda s: s.update(lastSwitchAt=h.clock() - 1.0))
        fleet = {
            "1": _usage(98.0) | {"seven_day": {"pct": 0.0}},  # headroom 2
            "2": _usage(50.0),                                # headroom 50
            "3": _usage(98.0) | {"seven_day": {"pct": 0.0}},  # headroom 2
        }
        outcome = h.tick_with_usage(fleet)
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert outcome is TickOutcome.SWITCHED, (
            f"got {outcome!r} ({reasons}) — an account the engine would "
            "refuse to LAND on must not be held by the cooldown"
        )
        assert h.active_number() == 2
        h.clock.advance(30.0)
        h.tick_with_usage(fleet)
        assert h.active_number() == 2, (
            f"landed back on {h.active_number()} — a spent account must "
            "never be returned to"
        )

    # -- R2: look ahead by looking more often ---------------------------

    def test_r2_the_sleep_is_bounded_inside_the_danger_band(self, temp_home):
        """One branch per assertion, merged because they differ only in the
        active's headroom and the strategy: in-band under `dynamic` is
        capped, out-of-band is not, and `consume-first` never is."""
        from claude_swap.autoswitch import DANGER_INTERVAL_S

        # `_respect_poll_plan` shortens a sleep to the store's own next-poll
        # time — orthogonal, best-effort, and it would mask the upper bounds
        # the two control assertions below rest on.
        cap = DANGER_INTERVAL_S * 1.1
        # ONE harness, re-engined for the last case: `Path.home()` is patched
        # to `temp_home` for the whole test, so a second harness rooted at a
        # SUBDIRECTORY would read this one's live account (see
        # `EngineHarness.__init__`).
        in_band_fleet = {
            "1": _usage(95.0) | {"seven_day": {"pct": 0.0}},  # headroom 5
            "2": _usage(50.0),
            "3": _usage(50.0),
        }
        h = self._harness(temp_home)
        with patch.object(AutoSwitchEngine, "_respect_poll_plan", lambda self, d: d):
            h.tick_with_usage(in_band_fleet)
            in_band = h.engine._next_delay(TickOutcome.NO_ACTION)

            # A tick that dies before the widening must keep the band it
            # last measured: the full interval is exactly wrong for the
            # tick whose reading failed, and the next observation can be
            # the wall.
            with patch.object(
                h.switcher, "usage_entries_by_account",
                side_effect=ClaudeSwitchError("collector down"),
            ):
                assert h.engine.tick() is TickOutcome.ERROR
            after_error = h.engine._next_delay(TickOutcome.ERROR)
            # …but it must not outlive the strategy it was measured under.
            h.engine.apply_strategy("consume-first")
            after_flip = h.engine._next_delay(TickOutcome.ERROR)
            h.engine.apply_strategy("dynamic")

            h.tick_with_usage({
                "1": _usage(50.0), "2": _usage(50.0), "3": _usage(50.0),
            })
            healthy = h.engine._next_delay(TickOutcome.NO_ACTION)

            h.settings = replace(h.settings, strategy="consume-first")
            h.engine = h._make_engine()
            h.tick_with_usage(in_band_fleet)
            consume_first = h.engine._next_delay(TickOutcome.NO_ACTION)

        assert in_band <= cap, (
            f"{in_band}s inside the danger band — the margin is "
            f"{SPENT_HEADROOM_PCT} points and the tick is 360s"
        )
        assert after_error <= cap, (
            f"{after_error}s after a failed tick — the band the last good "
            "reading measured must survive an errored one"
        )
        assert after_flip > cap, (
            f"{after_flip}s after flipping to `consume-first` — the band "
            "must not outlive the strategy it was measured under"
        )
        assert healthy > cap, f"{healthy}s — a healthy active keeps the interval"
        assert consume_first > cap, (
            f"{consume_first}s — `consume-first` must keep its own sleep"
        )
