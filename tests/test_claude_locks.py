"""Tests for the proper-lockfile-compatible Claude Code lock helpers."""

from __future__ import annotations

import errno
import logging
import os
import threading
import time
from pathlib import Path

import pytest

from tests.conftest import _advancing_clock, _thread_scoped_sleep, _crossing_clock

from claude_swap import claude_locks
from claude_swap.claude_locks import (
    claude_config_lock,
    claude_credentials_lock,
    config_lock_dir,
    credentials_lock_dir,
    proper_lockfile,
)
from claude_swap.exceptions import ClaudeCodeLockTimeout


def _assert_backed_off(slept, budget, *, least=3, remainder=0.05, what="clamp"):
    """Every arm of the retry loop must CLAMP to what is left AND back off.

    The clamp half is one-sided on its own: a list of zeros satisfies it, and
    a list of zeros is the hot spin these cases exist to forbid. Copying the
    clamp assertions per arm is how all three arms here went without
    the lower bound `test_locking` has carried for its own clamp since the
    same pass -- so the assertions live in one place instead. It is a bound on
    ITERATIONS in disguise: elapsed is `sum(sleeps) + n * 0.001`, so it is
    satisfied by ~0.0095s per attempt whatever the arm's constant is. Each arm
    keeps an attempt COUNT of its own for the shrink this cannot see.
    """
    assert len(slept) >= least, f"the instrument, not the code: {slept}"
    for left, seconds in slept:
        assert seconds <= max(left, 0.0), (
            f"slept {seconds}s with {left}s left — the {what} used `timeout`, "
            f"not what remains of it (all sleeps: {slept})"
        )
    assert min(left for left, _ in slept) < remainder, (
        f"the run must reach a remainder under {remainder}: {slept}"
    )
    assert sum(s for _, s in slept) >= budget * 0.9, (
        f"slept {sum(s for _, s in slept)}s of a {budget}s budget — the arm "
        f"spun instead of backing off: {slept}"
    )


def _raising(real, path, exc):
    """`real`, but raising `exc` for `path`. Scoped to the path on purpose:
    the module does `import os`, so patching `claude_locks.os.<fn>` patches
    the global one and an unscoped stub feeds the injection to every other
    caller in the process."""
    def stub(p, *a, **k):
        if not isinstance(p, int) and os.fspath(p) == os.fspath(path):
            raise exc
        return real(p, *a, **k)
    return stub


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    return tmp_path / "target.lock"


class TestProperLockfile:
    def test_acquire_creates_and_release_removes(self, lock_dir):
        with proper_lockfile(lock_dir):
            assert lock_dir.is_dir()
        assert not lock_dir.exists()

    def test_reacquire_after_release(self, lock_dir):
        with proper_lockfile(lock_dir):
            pass
        with proper_lockfile(lock_dir):
            assert lock_dir.is_dir()

    def test_contention_times_out(self, lock_dir):
        lock_dir.mkdir()  # fresh mtime = live holder
        start = time.monotonic()
        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(lock_dir, timeout=0.5):
                pass
        assert time.monotonic() - start < 5.0
        assert lock_dir.is_dir()  # the holder's lock is left alone

    def test_stale_lock_is_taken_over(self, lock_dir):
        lock_dir.mkdir()
        past = time.time() - 30
        os.utime(lock_dir, (past, past))
        with proper_lockfile(lock_dir, timeout=2.0):
            assert lock_dir.is_dir()
            # We own it now: mtime is fresh, not the 30s-old corpse.
            assert time.time() - lock_dir.stat().st_mtime < 5.0
        assert not lock_dir.exists()

    def test_release_tolerates_stolen_lock(self, lock_dir):
        with proper_lockfile(lock_dir):
            os.rmdir(lock_dir)  # simulate a stale-takeover by another process
        # No exception; nothing left behind.
        assert not lock_dir.exists()

    def test_toucher_keeps_mtime_fresh(self, lock_dir, monkeypatch):
        monkeypatch.setattr(claude_locks, "TOUCH_INTERVAL_S", 0.1)
        with proper_lockfile(lock_dir):
            past = time.time() - 30
            os.utime(lock_dir, (past, past))
            time.sleep(0.4)
            assert time.time() - lock_dir.stat().st_mtime < 10.0

    def _count_touches(self, monkeypatch, lock_dir, fail_first=None, fail_all=None):
        """Patch os.utime and count only the calls aimed at OUR lock.

        Keyed on the path, not on a global call counter: the module does
        `import os`, so patching `claude_locks.os.utime` patches the global
        one and any other caller in the process consumes an injected failure.
        """
        real = os.utime
        state = {"n": 0}

        def counting(path, *a, **k):
            if not isinstance(path, int) and os.fspath(path) == os.fspath(lock_dir):
                state["n"] += 1
                if fail_all is not None:
                    raise fail_all
                if fail_first is not None and state["n"] == 1:
                    raise fail_first
            return real(path, *a, **k)

        monkeypatch.setattr(claude_locks.os, "utime", counting)
        return state, real

    def test_a_transient_utime_failure_does_not_disarm_the_heartbeat(
            self, lock_dir, monkeypatch):
        """One failed touch must not stop the touching.

        The toucher returned on any OSError, so a single transient failure
        killed the heartbeat while the lock was still held — and after
        CONFIG_STALENESS_S any waiter legally steals it.
        """
        monkeypatch.setattr(claude_locks, "TOUCH_INTERVAL_S", 0.05)
        touches, real = self._count_touches(
            monkeypatch, lock_dir, fail_first=OSError("transient"))
        with proper_lockfile(lock_dir):
            past = time.time() - 30
            real(lock_dir, (past, past))
            time.sleep(0.4)
            assert touches["n"] > 1, "the toucher stopped after one failure"
            assert time.time() - lock_dir.stat().st_mtime < 10.0, (
                "the lock went stale while still held, so a waiter may steal it"
            )

    def test_a_transient_failure_does_not_claim_the_lock_is_about_to_be_stolen(
            self, lock_dir, monkeypatch, caplog):
        """The sentence is only true of a failure that outlives the window.

        "its mtime stops advancing, so a waiter may take it over as stale" is
        the warning's own justification, and the arm fired on the FIRST
        failure regardless — so a single hiccup that clears on the next tick
        printed it while the mtime went on advancing and nothing was ever at
        risk. Measured before this: 7 touches, a lock fresh throughout, one
        warning saying the opposite.

        Its sibling covers the persistent case with `fail_all`, so nothing
        stood between the two.
        """
        monkeypatch.setattr(claude_locks, "TOUCH_INTERVAL_S", 0.02)
        touches, real = self._count_touches(
            monkeypatch, lock_dir, fail_first=OSError("transient"))
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            with proper_lockfile(lock_dir):
                # REWOUND FIRST, like the sibling. Without it the mtime only
                # ever moves FORWARD from the mkdir, so `age` is bounded by
                # the hold (0.3s) whatever the toucher does and the premise
                # below cannot fail. Measured: with a toucher that calls
                # `utime` but never advances the mtime, both siblings go red
                # and this one stayed green.
                past = time.time() - 30
                real(lock_dir, (past, past))
                time.sleep(0.3)
                # INSIDE THE HOLD: the release removes the directory, so the
                # freshness this is about is unobservable afterwards.
                age = time.time() - lock_dir.stat().st_mtime

        assert touches["n"] > 2, "control: the toucher must have kept trying"
        assert age < 5.0, (
            "premise: the mtime kept advancing, so a theft warning here would "
            "have been FALSE -- if it had frozen, the silence below is correct "
            "for the wrong reason"
        )
        said = [r.getMessage() for r in caplog.records if "refresh" in r.getMessage()]
        assert said == [], (
            f"a hiccup that cleared was reported as imminent theft: {said}"
        )

    def test_a_hiccup_after_a_long_healthy_hold_is_still_silent(
            self, lock_dir, monkeypatch, caplog):
        """`last_ok` is what makes the persistence gate mean anything, and
        deleting that one line left the whole suite green.

        Every other case here holds the lock for LESS than `staleness`, so
        `time.time() - last_ok > staleness` is false whether or not `last_ok`
        ever advances -- verified where it cannot fail. This one holds it
        WELL past the window with every touch succeeding, then fails one:
        with `last_ok` advancing that is a hiccup and says nothing; without
        it, the gate compares against acquisition time and the cries-wolf
        warning comes straight back.
        """
        staleness = 0.15
        monkeypatch.setattr(claude_locks, "TOUCH_INTERVAL_S", 0.02)
        real = os.utime
        state = {"n": 0, "t0": None, "hiccuped": False}

        def fail_once_past_the_window(path, *a, **k):
            if os.fspath(path) == os.fspath(lock_dir):
                state["n"] += 1
                if state["t0"] is None:
                    state["t0"] = time.time()
                # BOUND TO THE WINDOW, NOT TO A COUNT. Keyed on the tenth
                # touch this needed the toucher to reach ten inside the hold,
                # which is a claim about the RUNNER: a loaded one ran eight,
                # the injection never fired, and the case failed on its own
                # premise while the behaviour was never exercised.
                elif not state["hiccuped"] and (
                        time.time() - state["t0"] > staleness):
                    state["hiccuped"] = True
                    raise PermissionError("injected: one hiccup")
            return real(path, *a, **k)

        monkeypatch.setattr(claude_locks.os, "utime", fail_once_past_the_window)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            # Below the hold, so the gate is genuinely reached -- but not so
            # close to TOUCH_INTERVAL_S that one scheduler stall reads as a
            # frozen `last_ok`. At 0.05 a single 30ms hiccup between two ticks
            # cries wolf on correct code; 0.15 leaves 130ms and still catches
            # the `last_ok` deletion, because the hold is 0.4s either way.
            with proper_lockfile(lock_dir, staleness=staleness):
                time.sleep(0.4)

        assert state["hiccuped"], (
            f"premise: the injected hiccup never fired ({state['n']} touch(es) "
            "ran), so nothing was held past the staleness window and the "
            "silence below would be correct for the wrong reason"
        )
        said = [r.getMessage() for r in caplog.records if "refresh" in r.getMessage()]
        assert said == [], (
            f"one hiccup after a long healthy hold was reported as imminent "
            f"theft: {said}"
        )

    def test_a_persistent_touch_failure_is_reported_once(
            self, lock_dir, monkeypatch, caplog):
        """A failure that never clears leaves the takeover unexplainable.

        Staying armed is right — the errno in hand is what separates transient
        from gone — but a failure that outlives the staleness window stops the
        mtime advancing, and a waiter then legitimately steals a lock that is
        still held, with nothing anywhere recording why. One warning costs a
        bool; one per attempt would bury it.

        OUTLIVING THE WINDOW is what the warning claims, so the window is
        shortened here rather than waiting `CONFIG_STALENESS_S` for it. With
        the default the failure never persists long enough and this case
        stopped discriminating the moment the arm learned to ask.
        """
        monkeypatch.setattr(claude_locks, "TOUCH_INTERVAL_S", 0.02)
        touches, _ = self._count_touches(
            monkeypatch, lock_dir, fail_all=PermissionError("injected"))
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            with proper_lockfile(lock_dir, staleness=0.05):
                time.sleep(0.3)

        assert touches["n"] > 1, "control: the toucher must have kept trying"
        said = [r for r in caplog.records if "refresh" in r.getMessage()]
        assert len(said) == 1, f"expected exactly one warning, got {len(said)}"

    @pytest.mark.parametrize("errno_", [errno.ESTALE, errno.EACCES, errno.EIO])
    def test_a_failure_that_is_not_absence_keeps_the_heartbeat(
            self, lock_dir, monkeypatch, errno_):
        """Only absence may stop the toucher; every other errno is transient.

        `os.stat` fails too, because that is what these errnos mean on a real
        filesystem — a stale NFS handle or an unreadable directory does not
        answer `stat` either. Injecting only a `utime` failure leaves the
        directory genuinely present, so any implementation that asks the
        filesystem gets a truthful "still there" and the test passes without
        exercising anything.

        With `stat` failing too, asking `lock_dir.exists()` is wrong on every
        supported Python and for two different reasons: 3.12 re-raises these
        errnos out of the thread as does 3.13, 3.14+ swallows them and answers False, which
        reads as "gone" for a lock we still hold. requires-python is >=3.12
        and CI runs 3.12, so neither half can be dismissed as theoretical.
        """
        monkeypatch.setattr(claude_locks, "TOUCH_INTERVAL_S", 0.05)
        real_utime, real_stat = os.utime, os.stat
        touches = {"n": 0}
        broken = {"on": False}

        def failing_utime(path, *a, **k):
            if not isinstance(path, int) and os.fspath(path) == os.fspath(lock_dir):
                touches["n"] += 1
                if not broken["on"]:
                    broken["on"] = True
                    raise OSError(errno_, "not absence")
            return real_utime(path, *a, **k)

        def failing_stat(path, *a, **k):
            if (broken["on"] and not isinstance(path, int)
                    and os.fspath(path) == os.fspath(lock_dir)):
                raise OSError(errno_, "not absence")
            return real_stat(path, *a, **k)

        monkeypatch.setattr(claude_locks.os, "utime", failing_utime)
        monkeypatch.setattr(claude_locks.os, "stat", failing_stat)
        with proper_lockfile(lock_dir):
            time.sleep(0.4)
            settled = touches["n"]
        assert broken["on"], (
            "the injected failure never fired — the instrument, not the code"
        )
        assert settled > 1, (
            f"errno {errno_} stopped the toucher after {settled} touch(es); "
            "the lock is still held and only absence may stop it"
        )

    def test_a_vanished_lock_still_stops_the_toucher(self, lock_dir, monkeypatch):
        """The control. Retrying forever on a lock that is GONE would be the
        opposite defect, and the original `return` existed for this case.

        Asserted on the TOUCHER, not on the directory: `os.utime` cannot
        create one, so `assert not lock_dir.exists()` passes for a toucher
        that never stops at all.
        """
        monkeypatch.setattr(claude_locks, "TOUCH_INTERVAL_S", 0.05)
        # COUNTED ON THE TICK, not on `os.utime`. WHERE a tick notices absence
        # is an implementation choice -- the leading stat is one syscall
        # earlier than the refresh -- and a utime counter reads zero for a
        # toucher that ran and correctly stopped, failing its own premise
        # about code that is right. What this case is about is that the LOOP
        # ends, so count the loop.
        # BOTH SYSCALLS, because either one can be the tick's first. A
        # heartbeat that verifies identity before refreshing notices absence
        # at the STAT and never reaches `utime`; one that refreshes straight
        # away notices it at the utime. Counting only the second reads zero
        # for a toucher that ran and stopped correctly.
        real = {"stat": os.stat, "utime": os.utime}
        ticks = {"n": 0}

        def counting(name):
            def call(path, *a, **k):
                if (not isinstance(path, int)
                        and os.fspath(path) == os.fspath(lock_dir)):
                    ticks["n"] += 1
                return real[name](path, *a, **k)
            return call

        with proper_lockfile(lock_dir):
            # Armed INSIDE the hold, so the acquire's own calls are not ticks.
            monkeypatch.setattr(claude_locks.os, "stat", counting("stat"))
            monkeypatch.setattr(claude_locks.os, "utime", counting("utime"))
            os.rmdir(lock_dir)
            time.sleep(0.15)
            settled = ticks["n"]
            assert settled >= 1, (
                "the toucher never ran in the window — the instrument, not "
                "the code (raise the sleep or lower TOUCH_INTERVAL_S)"
            )
            time.sleep(0.3)
            assert ticks["n"] == settled, (
                f"the toucher kept going on a dead lock "
                f"({ticks['n'] - settled} more attempts)"
            )

    def test_creates_missing_parent(self, tmp_path):
        nested = tmp_path / "a" / "b" / "target.lock"
        with proper_lockfile(nested):
            assert nested.is_dir()

    def test_the_rmdir_branch_sleeps_only_what_is_left(
        self, lock_dir, monkeypatch
    ):
        """A SCRIPTED CLOCK: the remaining budget at each sleep is CHOSEN.

        The two ~90-line cases this replaces raced for it. They anchored on
        the code's own `monotonic` and then asserted the run had ENTERED the
        region where a flat sleep is observable -- and `min(lefts)` is
        `budget mod flat`, so losing one loop iteration to latency raises it
        by a whole `flat` and the assertion can never be satisfied. Measured
        on pristine source with per-iteration latency injected: FAIL at 7ms,
        8ms, 25ms, 30ms, 80ms; PASS at 0, 6, 10, 12, 40. Under ordinary
        fair-share contention, 3 of 60 runs red.
        
        Deleting that assertion is not the fix either: without it a flat 0.05
        SURVIVES at 25ms of latency. The case was caught between passing the
        defect and failing on correct code. With the clock scripted there is
        no race to lose: every sleep is measured against a remainder this
        test decided, and the flat 0.005 the body called uncatchable dies too.
        """
        lock_dir.mkdir()
        os.utime(lock_dir, (0, 0))                    # ancient -> stale
        budget, clock, slept = 0.175, [0.0], []
        real_rmdir = os.rmdir

        def refuse(path, *a, **k):
            if os.fspath(path) == os.fspath(lock_dir):
                clock[0] += 0.001                     # one iteration of work
                raise OSError(errno.EACCES, "cannot remove")
            return real_rmdir(path, *a, **k)


        fake_sleep = _thread_scoped_sleep(claude_locks, clock, slept, budget)

        monkeypatch.setattr(claude_locks.os, "rmdir", refuse)
        monkeypatch.setattr(claude_locks.time, "monotonic", _advancing_clock(clock, budget))
        monkeypatch.setattr(claude_locks.time, "sleep", fake_sleep)

        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(lock_dir, timeout=budget):
                pass

        _assert_backed_off(slept, budget)

    def test_the_jitter_branch_sleeps_only_what_is_left(
        self, lock_dir, monkeypatch
    ):
        """THE SITE THIS PR IS NAMED AFTER, and it had no flat-sleep guard.

        Mutating only the jitter clamp to a flat sleep: 0.05 passed, 0.10
        passed, 0.14 passed — a 14x overshoot of the 0.01s timeout the PR's
        own opening table calls the bug — and only 0.16 failed. Every earlier
        flat-sleep row was measured with both sites mutated together, so all
        the signal came from the rmdir site.

        A HELD, FRESH lock takes this branch: the staleness test reads
        `time.time()`, which is left alone, and only `monotonic` is scripted.
        """
        lock_dir.mkdir()                              # held, and fresh
        # THE JITTER IS SCRIPTED TOO. It is randomness in the CODE, not in the
        # clock, so leaving it live makes the tail remainder vary per run and
        # the region assert below flaky -- measured 27 of 60 red before this.
        # Pinned to 0, the draw is a flat 0.25 and the budget is chosen so
        # `budget mod (0.25 + 0.001)` is 0.003: small enough that any flat
        # sleep overshoots it.
        monkeypatch.setattr(claude_locks.random, "random", lambda: 0.0)
        budget, clock, slept = 0.756, [0.0], []


        fake_sleep = _thread_scoped_sleep(claude_locks, clock, slept, budget, step=0.001)

        monkeypatch.setattr(claude_locks.time, "monotonic", _advancing_clock(clock, budget))
        monkeypatch.setattr(claude_locks.time, "sleep", fake_sleep)

        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(lock_dir, timeout=budget):
                pass

        _assert_backed_off(slept, budget, least=2, remainder=0.005,
                           what="jitter clamp")


class TestLockPaths:
    def test_default_paths(self, temp_home, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        assert credentials_lock_dir() == temp_home / ".claude.lock"
        assert config_lock_dir() == temp_home / ".claude.json.lock"

    def test_claude_config_dir_is_honored(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom-claude"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        assert credentials_lock_dir() == tmp_path / "custom-claude.lock"
        # ~/.claude.json resolves relative to CLAUDE_CONFIG_DIR too.
        assert config_lock_dir() == custom / ".claude.json.lock"

    def test_named_helpers_lock_their_dirs(self, temp_home, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        with claude_credentials_lock():
            assert (temp_home / ".claude.lock").is_dir()
            with claude_config_lock():
                assert (temp_home / ".claude.json.lock").is_dir()
        assert not (temp_home / ".claude.lock").exists()
        assert not (temp_home / ".claude.json.lock").exists()


class TestEveryArmOfTheLoopBacksOff:
    """The bounding pass clamped the two sleeping arms and skipped one.

    A lock PATH that is a dangling symlink answers `FileExistsError` to
    `mkdir` and `FileNotFoundError` to `stat`, so the retry took the one arm
    with no sleep in it, for the whole budget. Measured before this: 109,000
    mkdir attempts per second -- four times the spin this branch was written
    to bound, and reached with no race at all.
    """

    def test_a_held_fresh_lock_does_not_spin(self, tmp_path, monkeypatch):
        """THE ORDINARY CONTENDED PATH, and the arm this class is named for.

        The symlink case below covers the stat-FNF arm. The JITTER arm -- the
        one every normal waiter takes against a lock that is held and fresh --
        had no spin bound at all, so a clamp that evaluates negative sleeps
        zero and the loop runs flat out. Measured: `deadline - time.time()`
        instead of `time.monotonic()` (boot-relative against epoch, so the
        remainder is hugely negative and `max(0.0, ...)` yields 0) took the
        attempts in a 0.3s budget from 2 to 5135, with both lock files green.

        The bound is 2x the measured count, not three orders of magnitude:
        attempts are budget/sleep, so a loaded machine yields FEWER and the
        noise cannot push it up. A 40 tolerated a 20x shrink in silence.
        """
        lock = tmp_path / "held.lock"
        lock.mkdir()  # FRESH, so the stale-takeover arm is never entered
        tries = {"n": 0}
        real_mkdir = os.mkdir

        def counting(path, *a, **k):
            if os.fspath(path) == os.fspath(lock):
                tries["n"] += 1
            return real_mkdir(path, *a, **k)

        monkeypatch.setattr(claude_locks.os, "mkdir", counting)
        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(lock, timeout=0.3):
                pass

        assert tries["n"] >= 1, f"the instrument, not the code: {tries['n']}"
        # Attempts are NOT budget/sleep: the clamp is
        # `min(sleep, deadline - now)`, so the tail sleeps shrink toward zero
        # and the loop iterates fast as it approaches the deadline. The noise
        # therefore runs UPWARD too, by a few iterations, and by more on a
        # platform with a coarser timer. Measured on the sibling arm: 7 on
        # linux (12 of 12) against 11 on the windows job, where a bound of 10
        # refused a correct tree and blocked every deploy. A busy spin is
        # ~50,000 attempts in the same budget, so the headroom below costs no
        # discriminating power at all.
        assert tries["n"] <= 12, (
            f"{tries['n']} mkdir attempts in a 0.3s budget — the jittered "
            "arm is not sleeping, so a waiter pegs a core for the whole hold"
        )

    def test_the_backoff_is_jittered_so_waiters_do_not_synchronise(
        self, tmp_path, monkeypatch
    ):
        """A FLAT BACK-OFF PASSES EVERY COUNT BOUND IN THIS CLASS.

        Attempts are budget/sleep, so replacing `0.25 + random() * 0.25` with
        a flat `0.25` leaves the count identical and both spin bounds green.
        What the jitter buys is that waiters released together do not retry in
        lockstep, and only the SPREAD of the drawn values shows it.

        The clock is scripted because a real run of this budget takes the
        budget; the draws are what is under test, not the waiting.
        """
        lock = tmp_path / "held.lock"
        lock.mkdir()  # FRESH, so the stale-takeover arm is never entered

        budget = 3.0
        clock, slept = [0.0], []

        fake_sleep = _thread_scoped_sleep(claude_locks, clock, slept)

        # THE SHARED CLOCK, not a copy of its arithmetic. The last sleep is
        # clamped to what is left, so it is exactly 0.0 and a clock that moves
        # only inside `sleep` parks ON the deadline forever.
        fake_monotonic = _advancing_clock(clock, budget)

        monkeypatch.setattr(claude_locks.time, "sleep", fake_sleep)
        monkeypatch.setattr(claude_locks.time, "monotonic", fake_monotonic)
        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(lock, timeout=budget):
                pass

        # UNCLAMPED DRAWS ONLY. The final sleep is `min(draw, what is left)`,
        # which is a clamp rather than a draw, and a clamped tail could make
        # one value look like spread or hide its absence.
        draws = slept[:-1]
        assert len(draws) >= 5, f"too few draws to judge spread: {slept}"
        # THE SPREAD, NOT MERE DISTINCTNESS. `len(set(draws)) > 1` is satisfied
        # by any band at all: a jitter narrowed to a microsecond is lockstep in
        # every sense that matters and passed it. The band is 0.25 wide, and
        # over 3000 runs of this body the observed range had a minimum of
        # 0.044 and never fell below 0.02, so this threshold is ~2x below the
        # floor and kills both the deletion and a 25x narrowing.
        spread = max(draws) - min(draws)
        assert spread > 0.02, (
            f"the {len(draws)} back-offs span only {spread:.6f}s "
            f"(min {min(draws):.6f}, max {max(draws):.6f}) — the jitter is "
            "gone or narrowed, so waiters released together retry in lockstep"
        )

    def test_a_swept_name_does_not_spin(self, tmp_path, monkeypatch):
        target = tmp_path / "target.lock"
        target.mkdir()

        real_mkdir, real_stat = os.mkdir, os.stat
        tries = {"n": 0}

        def counting(path, *a, **k):
            if os.fspath(path) == os.fspath(target):
                tries["n"] += 1
            return real_mkdir(path, *a, **k)

        monkeypatch.setattr(claude_locks.os, "mkdir", counting)
        monkeypatch.setattr(claude_locks.os, "stat",
                            _raising(real_stat, target, FileNotFoundError(errno.ENOENT, "swept")))
        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(target, timeout=0.3):
                pass

        assert tries["n"] > 1, "premise: the loop must have retried at all"
        # 7 on linux, measured 12 of 12; 11 on the windows job, because the
        # clamp's tail iterates fast (see the jittered arm above). A busy
        # spin is ~50,000.
        assert tries["n"] <= 20, (
            f"{tries['n']} mkdir attempts in a 0.3s budget — the arm that "
            "retries a vanished name never sleeps, so it pins a core"
        )

    def test_the_rmdir_refusal_does_not_spin(self, tmp_path, monkeypatch):
        """The third arm's ATTEMPT COUNT, which its two siblings both have.

        The sleep-total bound is a bound on ITERATIONS in disguise: total
        elapsed on the scripted clock is `sum(sleeps) + n * 0.001`, so
        `sum >= 0.9 * budget` is satisfied by roughly 0.0095s per attempt
        whatever the arm's own constant is. A five-fold shrink of this arm's
        flat 0.05s therefore passes it. A count is what the other two arms use
        against exactly that, and this arm was the only one without one.
        """
        target = tmp_path / "target.lock"
        target.mkdir()
        stale = time.time() - 60
        os.utime(target, (stale, stale))

        tries = {"n": 0}

        real_rmdir = os.rmdir

        # SCOPED TO THIS PATH. The module does `import os`, so this patches
        # the GLOBAL `os.rmdir`: unscoped it feeds an injected EACCES to any
        # other caller in the process and counts THEIR removals into a bound
        # with four of slack. `_count_touches` states the same rule for
        # `os.utime` a few hundred lines up. Six other patches in this file
        # obey neither path nor thread, so this is the rule, not the habit.
        def refusing(path, *a, **k):
            if os.fspath(path) != os.fspath(target):
                return real_rmdir(path, *a, **k)
            tries["n"] += 1
            raise PermissionError(errno.EACCES, "cannot remove it either")

        monkeypatch.setattr(claude_locks.os, "rmdir", refusing)
        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(target, timeout=0.3, staleness=1.0):
                pass

        assert tries["n"] > 1, "premise: the loop must have retried at all"
        # 10, on the sibling's arithmetic MINUS ONE. A flat 0.05s over a 0.3s
        # budget is 6 attempts HERE: the sibling counts `mkdir`, which runs
        # before the deadline check and so gets one extra call on the final
        # iteration, while this counts `rmdir`, which runs after it. 10 leaves
        # headroom without tolerating the 5x shrink the sleep-total bound
        # cannot see. Same upward tail as its siblings.
        assert tries["n"] <= 20, (
            f"{tries['n']} rmdir attempts in a 0.3s budget — the arm that "
            "cannot remove a stale lock backed off less than it claims to"
        )

    def test_the_swept_name_arm_sleeps_only_what_is_left(
        self, tmp_path, monkeypatch
    ):
        """A SCRIPTED CLOCK, like its two siblings.

        NAMED FOR THE ARM IT REACHES. A dangling symlink answers
        FileExistsError to `mkdir`, so this drives the arm where the READ-BACK
        stat raises ENOENT -- not a name swept between `mkdir` and `stat`.
        The two are one `except` on this branch and two on the merged tree,
        where the swept-name arm has its own back-off that this case never
        touches.

        The wall-clock form this replaces could only see a FLATTENING. With a
        budget under the flat constant, `min(0.05, timeout)` and
        `min(0.05, remaining)` are the same number -- which is exactly the
        weakening this branch's clamps exist against. Measured on that form:
        clamping to `timeout` instead of the remainder left the suite at 43
        passed, and its lower bound could not fail at all, because the raise
        it waits for is itself gated on the deadline having passed.
        """
        target = tmp_path / "target.lock"
        target.mkdir()

        budget, clock, slept = 0.175, [0.0], []
        real_mkdir, real_stat = os.mkdir, os.stat

        def counting(path, *a, **k):
            if os.fspath(path) == os.fspath(target):
                clock[0] += 0.001                     # one iteration of work
            return real_mkdir(path, *a, **k)

        fake_sleep = _thread_scoped_sleep(claude_locks, clock, slept, budget)

        monkeypatch.setattr(claude_locks.os, "mkdir", counting)
        monkeypatch.setattr(claude_locks.os, "stat",
                            _raising(real_stat, target, FileNotFoundError(errno.ENOENT, "swept")))
        monkeypatch.setattr(claude_locks.time, "monotonic", _advancing_clock(clock, budget))
        monkeypatch.setattr(claude_locks.time, "sleep", fake_sleep)

        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(target, timeout=budget):
                pass

        _assert_backed_off(slept, budget)


class TestTheTakeoverGuardIsInsideTheTimeout:
    """`timeout` bounds the whole call, and the guard is the one place it did not.

    The clamped sleeps in the retry loop exist so a deadline crossed
    mid-iteration cannot overrun. The takeover's own `FileLock` was handed a
    fixed wait instead of the remaining budget, so a contended guard added up
    to its own timeout ON TOP, once per call -- and a single switch takes
    three of these locks.
    """

    def test_a_contended_guard_does_not_outlive_the_budget(self, tmp_path, monkeypatch):
        target = tmp_path / "target.lock"
        target.mkdir()
        stale = time.time() - 100
        os.utime(target, (stale, stale))
        guard = target.parent / f"{target.name}.takeover"

        # `elapsed` cannot tell the clamped guard from the jitter arm's own
        # clamp beside it, so the number alone does not say which arm produced
        # it. Recording the budgets ties it to this arm AND to the regime in
        # which the two forms differ.
        budgets = []
        real_take_over = claude_locks._take_over_stale

        def recording(*a, **k):
            budgets.append(k["budget"])
            return real_take_over(*a, **k)

        monkeypatch.setattr(claude_locks, "_take_over_stale", recording)

        # Past the first iteration, which costs the cap plus the declined-
        # takeover back-off. Only the SECOND call separates the two forms.
        budget = claude_locks._TAKEOVER_GUARD_S + 0.2
        margin = 0.15

        held = threading.Event()
        release = threading.Event()

        def hold():
            # OUTLASTS THE BUDGET, which is derived from the cap: a fixed hold
            # is a second small-side band. Measured, at a 5s hold and a cap of
            # 5.0 the peer let go mid-run, the takeover succeeded, and the case
            # reported `DID NOT RAISE` -- the shape of the very defect this
            # branch fixes, on correct code.
            with claude_locks.FileLock(guard, timeout=5):
                held.set()
                release.wait(budget + 5)

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        assert held.wait(5), "premise: the peer must hold the guard"
        try:
            started = time.monotonic()
            with pytest.raises(ClaudeCodeLockTimeout):
                with proper_lockfile(target, timeout=budget, staleness=1.0):
                    pass
            elapsed = time.monotonic() - started
        finally:
            release.set()
            t.join(5)

        # THE REGIME, as the INEQUALITY rather than its solved value: clamped
        # costs `budget`, unclamped `2*cap + the declined-takeover back-off`,
        # and they must separate by more than `margin`. Writing the solved
        # `> 0.30` hid that it is a function of all three, so moving the margin
        # or the budget sizing re-opened the blind band in silence. Everything
        # here is a constant the code under test cannot move, so this stays a
        # precondition rather than a detector wearing one's label. The back-off
        # is READ, not guessed, so no value of it leaves a band.
        assert (
            2 * claude_locks._TAKEOVER_GUARD_S + claude_locks._DECLINE_BACKOFF_S
            > budget + margin
        ), (
            "premise: the cap is too small for the clamped and unclamped forms "
            "to separate by more than the margin this case allows"
        )
        assert len(budgets) >= 2, (
            "premise: only one iteration ran, and on the first one "
            f"`min(cap, remaining)` and `min(cap, timeout)` agree: {budgets}"
        )
        assert elapsed < budget + margin, (
            f"waited {elapsed:.3f}s on a {budget}s budget -- the takeover "
            f"guard is not clamped to the remaining time"
        )

    def test_the_guard_waits_no_longer_than_the_guard_constant(
        self, tmp_path, monkeypatch
    ):
        """A default of `_TAKEOVER_GUARD_S` would freeze the import value."""
        seen = []
        real = claude_locks.FileLock

        def recording(path, **kw):
            seen.append(kw["timeout"])
            return real(path, **kw)

        monkeypatch.setattr(claude_locks, "FileLock", recording)
        monkeypatch.setattr(claude_locks, "_TAKEOVER_GUARD_S", 7.0)
        gone = tmp_path / "gone.lock"
        assert claude_locks._take_over_stale(gone, 60.0, budget=60.0) is True
        assert seen == [7.0], f"the guard waited {seen} under a cap of 7.0"

class TestADeadlineCanPassMidIterationForEveryArm:
    """The floor under each arm's clamp, which nothing here exercised.

    Same hazard as `locking.py`'s witnessed one (`TestTheDeadlineCanPassBetween
    TheCheckAndTheClamp` in ``test_locking.py``): the loop checks the deadline,
    then reads the clock again to size the sleep, and a `stat`/`rmdir` syscall
    can land between the two. When the budget expires in that gap,
    `deadline - time.monotonic()` goes negative and an unfloored `time.sleep`
    raises ValueError -- undocumented by `proper_lockfile`, which documents
    itself as raising only `ClaudeCodeLockTimeout`.

    Every clamp case above scripts `time.sleep` itself, so the real function
    -- and the ValueError it would raise on a negative duration -- never runs.
    These instead script only `time.monotonic`, leaving the real `time.sleep`
    in place, so a deleted floor is a genuine crash here.
    """

    def test_a_deadline_crossed_mid_swept_name_arm_is_not_a_ValueError(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "target.lock"
        target.mkdir()

        real_stat = os.stat

        reads = []
        monkeypatch.setattr(claude_locks.os, "stat",
                            _raising(real_stat, target, FileNotFoundError(errno.ENOENT, "swept")))
        monkeypatch.setattr(claude_locks.time, "monotonic", _crossing_clock(reads))
        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(target, timeout=0.5):
                pass
        assert reads == [0.0, 0.0, 0.6, 0.6], (
            f"the swept-name arm's clamp was never entered: {reads}"
        )

    def test_a_deadline_crossed_mid_rmdir_failed_arm_is_not_a_ValueError(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "target.lock"
        target.mkdir()
        stale = time.time() - 100
        os.utime(target, (stale, stale))

        real_rmdir = os.rmdir
        refused = {"n": 0}

        def refusing(path, *a, **k):
            if os.fspath(path) == os.fspath(target):
                refused["n"] += 1
                raise PermissionError(errno.EACCES, "cannot remove it either")
            return real_rmdir(path, *a, **k)

        monkeypatch.setattr(claude_locks.os, "rmdir", refusing)

        monkeypatch.setattr(claude_locks.time, "monotonic", _crossing_clock([]))
        # THE RAISE IS THE ASSERTION: unclamped, this arm reaches `time.sleep`
        # with a negative value, and that is a ValueError this `raises` would
        # not accept. Pinning the READ SEQUENCE instead only detects change --
        # it was already bumped once for a correct one. The clock crossing is
        # not asserted because the raise implies it: the deadline check is the
        # only site that raises, and it needs a read past the deadline.
        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(target, timeout=0.5, staleness=1.0):
                pass
        assert refused["n"] >= 1, "premise: the rmdir-failed arm never ran"

    def test_a_deadline_crossed_mid_jitter_arm_is_not_a_ValueError(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "target.lock"
        target.mkdir()  # FRESH mtime -> contended, not stale -> jitter branch

        monkeypatch.setattr(claude_locks.random, "random", lambda: 0.0)

        reads = []
        monkeypatch.setattr(claude_locks.time, "monotonic", _crossing_clock(reads))
        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(target, timeout=0.5):
                pass
        assert reads == [0.0, 0.0, 0.6, 0.6], (
            f"the jitter arm's clamp was never entered: {reads}"
        )


def test_a_second_freeze_after_a_recovery_is_reported_again(
    tmp_path, monkeypatch, caplog
):
    """The latch is "once per FREEZE", not "once per hold".

    `warned` is set and never cleared, so a freeze that outlives `staleness`,
    RECOVERS, and then freezes again is silent the second time -- and the
    second one is a takeover the log would otherwise explain. The latch exists
    to stop a per-attempt repeat inside ONE episode; a recovery ends that
    episode, and `last_ok` moving is exactly what says so.
    """
    lock = tmp_path / "target.lock"
    monkeypatch.setattr(claude_locks, "TOUCH_INTERVAL_S", 0.01)
    failing = {"on": True}
    real_utime = os.utime

    def flaky(path, *a, **k):
        if failing["on"] and os.fspath(path) == os.fspath(lock):
            raise OSError(errno.EIO, "injected")
        return real_utime(path, *a, **k)

    monkeypatch.setattr(claude_locks.os, "utime", flaky)
    with caplog.at_level(logging.WARNING, logger="claude-swap"):
        with proper_lockfile(lock, timeout=2.0, staleness=0.05):
            time.sleep(0.2)                       # episode one, outlives staleness
            failing["on"] = False
            time.sleep(0.1)                       # recovery: last_ok moves again
            failing["on"] = True
            time.sleep(0.2)                       # episode two
    said = [r.getMessage() for r in caplog.records if "stops advancing" in r.getMessage()]
    assert len(said) == 2, (
        f"{len(said)} warning(s) for two separate freezes — a freeze that "
        "recovered and returned is the one the takeover follows, and the "
        "latch swallowed it"
    )


class TestTheStaleTakeoverDoesNotRemoveASuccessorsLock:
    """`os.stat` decides and `os.rmdir` acts; a peer can win the same race
    in between and create ITS lock at this name. Removing that puts two
    processes in the critical section at once."""

    def test_a_corpse_is_removed_and_a_fresh_lock_is_left_alone(self, tmp_path):
        lock_dir = tmp_path / "target.lock"
        staleness = 60.0

        # CONTROL, and the positive arm: a genuine corpse IS taken over, so
        # a False below cannot be a takeover that never works at all.
        lock_dir.mkdir()
        past = time.time() - 10 * staleness
        os.utime(lock_dir, (past, past))
        assert claude_locks._take_over_stale(lock_dir, staleness, budget=60.0) is True
        assert not lock_dir.exists()

        # THE ARM UNDER TEST: by the time the removal runs, a peer has
        # retaken the name. Its directory is fresh, and removing it would
        # leave that peer holding a lock this process is about to recreate.
        lock_dir.mkdir()
        assert claude_locks._take_over_stale(lock_dir, staleness, budget=60.0) is False, (
            "DEFECT: a directory that is no longer stale was removed -- a "
            "peer that won the takeover race holds it, and taking it away "
            "puts both processes inside the critical section"
        )
        assert lock_dir.exists(), "the successor's lock must survive"

    def test_the_decide_and_remove_window_is_exclusive(self, tmp_path):
        """The re-read is only half the fix; the other half is that two
        waiters cannot be inside this window at once.

        Without the exclusion the re-read still reads a corpse in both
        processes, both remove it, and the second one removes whatever the
        first has already created.
        """
        from claude_swap.locking import FileLock

        lock_dir = tmp_path / "target.lock"
        staleness = 60.0
        lock_dir.mkdir()
        past = time.time() - 10 * staleness
        os.utime(lock_dir, (past, past))
        guard = lock_dir.parent / f"{lock_dir.name}.takeover"

        peer = FileLock(guard, timeout=0.5)
        assert peer.acquire(), "premise: the peer must hold the guard first"
        try:
            assert claude_locks._take_over_stale(lock_dir, staleness, budget=60.0) is False, (
                "DEFECT: a second waiter entered the decide-and-remove window "
                "while a peer was inside it. Both then remove the corpse and "
                "the loser's fresh lock goes with it, putting two processes "
                "in the critical section"
            )
            assert lock_dir.exists(), "the corpse must be left for the peer"
        finally:
            peer.release()

        # CONTROL: with the guard free the SAME corpse is taken over, so the
        # False above cannot be a takeover that never works at all.
        assert claude_locks._take_over_stale(lock_dir, staleness, budget=60.0) is True
        assert not lock_dir.exists()

    def test_a_vanished_lock_is_free_to_take(self, tmp_path):
        """The caller's next mkdir decides; a False here cost it a back-off
        before a mkdir that would have succeeded."""
        assert claude_locks._take_over_stale(tmp_path / "gone.lock", 60.0, budget=60.0) is True

    def test_a_lock_that_vanishes_at_the_rmdir_is_free_too(self, tmp_path, monkeypatch):
        """Absence is absence whichever syscall meets it.

        The stat arm above says so; the rmdir arm is the same fact one
        `except` later, and it answered False -- so a corpse that Claude Code
        (which takes no lock of ours) swept between our stat and our rmdir
        cost a back-off before a mkdir that would have succeeded, and handed
        the name to whoever was not backing off.
        """
        lock_dir = tmp_path / "target.lock"
        lock_dir.mkdir()
        past = time.time() - 600
        os.utime(lock_dir, (past, past))
        real_rmdir = os.rmdir

        def swept_first(path, *a, **k):
            if os.fspath(path) == os.fspath(lock_dir):
                real_rmdir(path)  # it really does go: the name IS free after
                raise FileNotFoundError(errno.ENOENT, "swept before our rmdir")
            return real_rmdir(path, *a, **k)

        monkeypatch.setattr(claude_locks.os, "rmdir", swept_first)
        got = claude_locks._take_over_stale(lock_dir, 60.0, budget=60.0)
        assert not lock_dir.exists(), "premise: the name must actually be free"
        assert got is True, (
            "the rmdir arm reported a free name as taken; the caller backs "
            "off before a mkdir that would have succeeded"
        )


class TestCcRefreshLockProtocol:
    """Claude Code 2.1.218 guards its OAuth refresh with TWO locks —
    ``<config-home>/.oauth_refresh.lock`` (primary) then the legacy
    ``<config-home>.lock`` — both at a 60s staleness. cswap must follow the
    same protocol or mutual exclusion silently fails (extracted from the
    2.1.218 bundle: ``uKi``/``CKi``, ``stale: 60000, update: 5000``)."""

    def test_oauth_refresh_lock_dir_default(self, temp_home, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        assert (
            claude_locks.oauth_refresh_lock_dir()
            == temp_home / ".claude" / ".oauth_refresh.lock"
        )

    def test_oauth_refresh_lock_dir_honors_claude_config_dir(
        self, tmp_path, monkeypatch
    ):
        custom = tmp_path / "custom-claude"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        assert claude_locks.oauth_refresh_lock_dir() == custom / ".oauth_refresh.lock"

    def test_credentials_lock_takes_both_locks(self, temp_home, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        new = temp_home / ".claude" / ".oauth_refresh.lock"
        legacy = temp_home / ".claude.lock"
        with claude_credentials_lock():
            assert new.is_dir(), "primary .oauth_refresh.lock not held"
            assert legacy.is_dir(), "legacy .claude.lock not held"
        assert not new.exists()
        assert not legacy.exists()

    @pytest.mark.parametrize("stale_at", [None, "primary", "legacy"])
    def test_the_primary_lock_is_taken_before_the_legacy_one(
        self, temp_home, monkeypatch, stale_at
    ):
        """The order is the whole point, and the two contention cases
        cannot see it: each asserts a lock is ABSENT after the `with`,
        which the release guarantees whichever order they were taken in.
        Claude Code takes the primary first and releases it on a legacy
        ELOCKED; taken the other way round, cswap holds the legacy lock
        while CC is still trying for it and burns CC's whole retry budget
        instead of failing it cheaply on the primary.

        One stale corpse at either path -- exactly what `_take_over_stale`
        exists to handle -- makes that path's `mkdir` run twice, so the
        recorder must count what SUCCEEDED. Recording the ATTEMPT put a
        third entry in the list and fired the premise below on an ordinary
        retry, reporting a broken acquisition order for a run that had one.
        """
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        corpse = {
            None: None,
            "primary": temp_home / ".claude" / ".oauth_refresh.lock",
            "legacy": temp_home / ".claude.lock",
        }[stale_at]
        if corpse is not None:
            corpse.mkdir(parents=True)
            past = time.time() - 100  # past CREDENTIALS_STALENESS_S
            os.utime(corpse, (past, past))

        created = []
        real_mkdir = claude_locks.os.mkdir

        def recording(path, *a, **k):
            result = real_mkdir(path, *a, **k)
            created.append(str(path))  # what SUCCEEDED, not what was tried
            return result

        monkeypatch.setattr(claude_locks.os, "mkdir", recording)
        with claude_credentials_lock(timeout=2.0):
            pass

        locks = [p for p in created if p.endswith(".lock")]
        # PREMISE: both locks were taken, or the order below is vacuous.
        assert len(locks) == 2, f"premise: both locks must be taken, got {locks}"
        assert locks[0].endswith(".oauth_refresh.lock"), (
            "DEFECT: the primary must be taken FIRST, as Claude Code does; "
            f"the order was {locks}"
        )

    def test_primary_contention_never_touches_legacy(self, temp_home, monkeypatch):
        """CC's order: primary first. If the primary is held we must time out
        without ever creating the legacy lock."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        new = temp_home / ".claude" / ".oauth_refresh.lock"
        new.mkdir(parents=True)  # fresh mtime = live CC holding its refresh lock
        with pytest.raises(ClaudeCodeLockTimeout):
            with claude_credentials_lock(timeout=0.5):
                pass
        assert not (temp_home / ".claude.lock").exists()
        assert new.is_dir()  # holder's lock untouched

    def test_legacy_contention_releases_primary(self, temp_home, monkeypatch):
        """If the legacy lock is contended after the primary was acquired,
        the primary must not be left behind (CC releases its new lock on
        legacy ELOCKED)."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        legacy = temp_home / ".claude.lock"
        legacy.mkdir()  # fresh = held
        with pytest.raises(ClaudeCodeLockTimeout):
            with claude_credentials_lock(timeout=0.5):
                pass
        assert not (temp_home / ".claude" / ".oauth_refresh.lock").exists()
        assert legacy.is_dir()

    def test_credentials_staleness_is_60s_not_10s(self, temp_home, monkeypatch):
        """A 30s-old credential lock belongs to a live CC (its budget is 60s)
        and must NOT be stolen — the old 10s staleness stole it."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        new = temp_home / ".claude" / ".oauth_refresh.lock"
        new.mkdir(parents=True)
        past = time.time() - 30
        os.utime(new, (past, past))
        with pytest.raises(ClaudeCodeLockTimeout):
            with claude_credentials_lock(timeout=0.5):
                pass
        assert new.is_dir()

    def test_the_legacy_lock_carries_the_60s_staleness_too(
        self, temp_home, monkeypatch
    ):
        """SECOND WITNESS. The two locks are separate calls with a staleness
        argument each, and the case above backdates only the primary -- so it
        times out there and the legacy call is never reached. Measured BEFORE
        this case existed: with the legacy call's staleness dropped to
        CONFIG_STALENESS_S the whole suite stayed green, while the same edit
        on the primary failed the case above. A 30s-old legacy lock is a live
        CC's, and stealing it puts a swap inside CC's refresh window.
        """
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        legacy = temp_home / ".claude.lock"
        legacy.mkdir()
        past = time.time() - 30
        os.utime(legacy, (past, past))
        with pytest.raises(ClaudeCodeLockTimeout):
            with claude_credentials_lock(timeout=0.5):
                pass
        assert legacy.is_dir()

    def test_credentials_lock_stale_past_60s_is_taken_over(
        self, temp_home, monkeypatch
    ):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        new = temp_home / ".claude" / ".oauth_refresh.lock"
        legacy = temp_home / ".claude.lock"
        new.mkdir(parents=True)
        legacy.mkdir()
        past = time.time() - 70
        os.utime(new, (past, past))
        os.utime(legacy, (past, past))
        with claude_credentials_lock(timeout=2.0):
            assert new.is_dir()
            assert legacy.is_dir()
        assert not new.exists()
        assert not legacy.exists()

    def test_config_lock_staleness_stays_10s(self, temp_home, monkeypatch):
        """The config lock's CC-side defaults are unchanged — a 30s-old
        config lock is still stale and taken over."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        cfg = temp_home / ".claude.json.lock"
        cfg.mkdir()
        past = time.time() - 30
        os.utime(cfg, (past, past))
        with claude_config_lock(timeout=2.0):
            assert cfg.is_dir()
        assert not cfg.exists()
