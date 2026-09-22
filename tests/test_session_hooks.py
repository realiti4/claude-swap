"""The managed-session prompt hooks (Refs #382)."""

from __future__ import annotations

import io
import json
import logging
import os
import select
import subprocess
import sys
import time
from dataclasses import replace
from unittest.mock import patch

import pytest

from claude_swap import (
    autoswitch,
    macos_keychain,
    oauth,
    paths,
    process_detection,
    session_hooks,
)
from claude_swap.credentials import CLAUDE_CODE_KEYCHAIN_SERVICE
from claude_swap.exceptions import LockError
from claude_swap.managed_sessions import (
    SOURCE_LANE0,
    AccountRef,
    ManagedSessionRegistry,
    create_managed_profile,
)
from claude_swap.session_credentials import WriteResult
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import FetchRecord, UsageEntry, UsageStore

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="managed sessions are POSIX-only (v1)"
)

# The managed_switcher fixture's three accounts, by slot. Lane 0 is #1.
LANE0 = AccountRef("lane0@example.com", "org-1")
B = AccountRef("b@example.com", "org-2")


def _usage_dict(pct: float) -> dict:
    return {"five_hour": {"pct": pct}, "seven_day": {"pct": pct}}


def _seed_usage(switcher, *, pct: float, number: str = "2", age_s: float = 0.0) -> None:
    """Store one account's usage as a fetch that landed ``age_s`` ago."""
    account = LANE0 if number == "1" else B
    store = UsageStore(
        switcher.backup_dir / "cache", clock=lambda: time.time() - age_s
    )
    store.record(
        {number: FetchRecord(usage=_usage_dict(pct))},
        {number: (account.email, account.organization_uuid)},
    )


def _entries(*, pct: float, age_s: float = 0.0, only: str | None = None):
    """``pct`` for ``only`` (or for every account), a healthy 1% for the rest."""
    out = {}
    for num in ("1", "2", "3"):
        value = pct if only is None or num == only else 1.0
        out[num] = UsageEntry(
            last_good=_usage_dict(value), fetched_at=time.time() - age_s, age_s=age_s
        )
    return out


def _serve_usage(monkeypatch, **kw) -> list:
    """Stand in for the fleet usage pass and record each ``fetch`` argument.

    Patched on the class, not on the fixture's instance: the hook builds a
    switcher of its own, which is the whole point of the gate above it.
    """
    asked: list = []

    def entries(self, fetch=None, **_kw):
        asked.append(fetch)
        return _entries(**kw)

    monkeypatch.setattr(ClaudeAccountSwitcher, "usage_entries_by_account", entries)
    return asked


def _moved(entry, decision):
    from claude_swap.session_reassign import ReassignResult

    return ReassignResult(
        entry.session_id, decision.reason, decision.placement.number,
        entry.account, decision.placement.account, True, "moved",
    )


def _register(
    switcher, account, *, source="backup", expires_in_ms=6 * 3600 * 1000,
    status="busy", idle_minutes=0.0, pid=None,
):
    """A live managed session on ``account`` whose profile holds a token."""
    registry = ManagedSessionRegistry(switcher.backup_dir)
    session_id = "auto-aaaaaaaa"
    entry = registry.allocate(
        session_id, lambda _busy: (account, source),
        pid=os.getpid() if pid is None else pid, proc_start=None,
    )
    session_dir = registry.session_dir(session_id)
    create_managed_profile(session_dir)
    # Claude's own record for this instance: without one the move rules
    # refuse, since a session Claude has not registered may still be
    # reading the credential its launch wrote.
    (session_dir / "sessions").mkdir(parents=True, exist_ok=True)
    (session_dir / "sessions" / f"{entry.pid}.json").write_text(json.dumps({
        "pid": entry.pid, "status": status, "cwd": "/work",
        "statusUpdatedAt": int((time.time() - idle_minutes * 60.0) * 1000),
    }))
    (session_dir / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "at-held",
            "expiresAt": int(time.time() * 1000) + expires_in_ms,
        },
    }))
    return switcher, registry, entry, session_dir


@pytest.fixture
def session(managed_switcher):
    """A managed session on account 2, with a fresh access-only credential."""
    return _register(managed_switcher, B)


def _env(session_dir):
    return {"CLAUDE_CONFIG_DIR": str(session_dir)}


# The two recheck bounds, spelled out here rather than read off the module:
# a test that ages a sentinel by the very constant it is testing shrinks
# with it, and a bound retuned to a second would still look right.
LANE0_RECHECK_S = 60.0
MOVE_RECHECK_S = 60.0


def test_the_recheck_bounds_are_what_the_tests_below_assume():
    assert session_hooks._LANE0_RECHECK_S == LANE0_RECHECK_S
    assert session_hooks._MOVE_RECHECK_S == MOVE_RECHECK_S


def _age_sentinel(session_dir, *, by: float | None = None) -> None:
    """Backdate the lane-0 sentinel past the interval it is believed for."""
    old = time.time() - (by or LANE0_RECHECK_S + 1.0)
    os.utime(session_dir / session_hooks.LANE0_SENTINEL, (old, old))


def _token(session_dir) -> str:
    raw = json.loads((session_dir / ".credentials.json").read_text())
    return raw["claudeAiOauth"]["accessToken"]


class TestEnsureFastPath:
    def test_an_unmanaged_config_dir_is_a_no_op(self, managed_switcher, tmp_path):
        assert session_hooks.run_ensure(env={"CLAUDE_CONFIG_DIR": str(tmp_path)}) == (
            "not-managed"
        )

    def test_no_config_dir_is_a_no_op(self, managed_switcher):
        assert session_hooks.run_ensure(env={}) == "not-managed"

    def test_a_row_the_sweep_dropped_stops_the_pass(self, session):
        _switcher, registry, entry, session_dir = session
        registry.remove(entry.session_id)
        assert session_hooks.run_ensure(env=_env(session_dir)) == "no-entry"

    def test_a_fresh_token_with_headroom_stops_before_the_expensive_work(
        self, session, monkeypatch
    ):
        """The gate is the whole design, so the property to assert is what
        the common prompt did NOT do.

        Building a switcher is what opens the log, runs the migrations and
        unlocks the roster-wide credential reads behind the fleet usage
        pass; spawning a process is what `ps` liveness probes and the
        keychain do. Neither may happen on a prompt that decides nothing,
        so both stand in here for every cost past the gate.
        """
        switcher, _registry, _entry, session_dir = session
        _seed_usage(switcher, pct=10.0)

        def no_switcher(self, *a, **kw):
            raise AssertionError("the fast path built a switcher")

        def no_process(*a, **kw):
            raise AssertionError("the fast path spawned a process")

        def no_tls(*a, **kw):
            raise AssertionError("the fast path installed the TLS verifier")

        monkeypatch.setattr(ClaudeAccountSwitcher, "__init__", no_switcher)
        monkeypatch.setattr("subprocess.run", no_process)
        monkeypatch.setattr("claude_swap.tls.use_native_tls", no_tls)
        assert session_hooks.run_ensure(env=_env(session_dir)) == "fresh"

    def test_usage_nobody_could_read_is_never_comfortable(self, session, monkeypatch):
        """Stale usage leaves the headroom unknown, and an account hosting a
        live session whose usage nobody has read is exactly the one worth
        looking at — so the gate opens."""
        switcher, _registry, _entry, session_dir = session
        _seed_usage(switcher, pct=10.0, age_s=600.0)
        asked = _serve_usage(monkeypatch, pct=10.0)
        assert session_hooks.run_ensure(env=_env(session_dir)) == "fresh"
        assert asked == [{"2"}]


class TestEnsureRegistryReads:
    def test_a_registry_it_cannot_read_is_not_warned_about_on_every_prompt(
        self, session, unconfigured_logger
    ):
        """This runs on every prompt of every managed session. A warning
        here would repeat several times a minute for as long as the file
        stayed corrupt, and push the engine's own history out of the log."""
        _switcher, _registry, _entry, session_dir = session
        (session_dir.parent / "managed.json").write_text("{ not json")
        with patch("claude_swap.session_hooks.os.environ", _env(session_dir)):
            assert session_hooks.ensure() == 0
        assert session_hooks.run_ensure(env=_env(session_dir)) == (
            "registry-unreadable"
        )
        log = _log_text()
        assert "managed.json" not in log
        assert "session ensure: registry-unreadable" not in log

    def test_a_row_that_is_simply_gone_reads_as_gone(self, session):
        _switcher, registry, entry, session_dir = session
        registry.remove(entry.session_id)
        assert session_hooks.run_ensure(env=_env(session_dir)) == "no-entry"


class TestEnsureRefresh:
    def test_a_token_inside_the_buffer_is_refreshed(self, managed_switcher):
        switcher, registry, entry, session_dir = _register(
            managed_switcher, B, expires_in_ms=60_000
        )
        _seed_usage(switcher, pct=10.0)
        assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"
        creds = json.loads((session_dir / ".credentials.json").read_text())
        assert creds["claudeAiOauth"]["accessToken"] == "at-2"
        assert "refreshToken" not in creds["claudeAiOauth"]
        assert registry.get(entry.session_id).access_fingerprint

    def test_the_refresh_records_where_the_token_came_from(self, managed_switcher):
        """The row names the holder this session last copied from, which is
        what tells the next pass its token is borrowed rather than its own."""
        switcher, registry, entry, session_dir = _register(
            managed_switcher, LANE0, expires_in_ms=60_000
        )
        _seed_usage(switcher, pct=10.0, number="1")
        assert entry.source == "backup"
        assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"
        assert registry.get(entry.session_id).source == SOURCE_LANE0
        assert _token(session_dir) == "at-live-1"

    def test_a_missing_credential_file_is_refreshed(self, session):
        switcher, _registry, _entry, session_dir = session
        _seed_usage(switcher, pct=10.0)
        (session_dir / ".credentials.json").unlink()
        assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"
        assert _token(session_dir) == "at-2"

    def test_a_refused_write_reports_it(self, session):
        switcher, _registry, _entry, session_dir = session
        _seed_usage(switcher, pct=10.0)
        (session_dir / ".credentials.json").unlink()
        with patch(
            "claude_swap.session_hooks.write_session_credential",
            return_value=WriteResult(False, "lock-timeout"),
        ):
            assert session_hooks.run_ensure(env=_env(session_dir)) == (
                "refresh-failed:lock-timeout"
            )

    def test_a_borrowed_token_is_recopied_when_lane_0_rotates(self, managed_switcher):
        """Lane 0's Claude rotates its own grant whenever it likes, and the
        copy this session holds keeps its own expiry either way — so the
        stored expiry cannot be what decides. The current token is."""
        switcher, _registry, _entry, session_dir = _register(
            managed_switcher, LANE0, source=SOURCE_LANE0
        )
        _seed_usage(switcher, pct=10.0, number="1")
        assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"
        assert _token(session_dir) == "at-live-1"

        (switcher.home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "at-live-2", "refreshToken": "rt-live-2",
                "expiresAt": int(time.time() * 1000) + 6 * 3600 * 1000,
            },
        }))
        assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"
        assert _token(session_dir) == "at-live-2"

    def test_a_borrowed_token_lane_0_still_serves_takes_the_fast_path(
        self, managed_switcher, monkeypatch
    ):
        """Checking a borrowed token must not cost the gate on every prompt:
        within the interval since the last authoritative answer, lane 0's
        plaintext credential and a hash are enough to stop as cheaply as any
        other session."""
        switcher, _registry, _entry, session_dir = _register(
            managed_switcher, LANE0, source=SOURCE_LANE0
        )
        _seed_usage(switcher, pct=10.0, number="1")
        assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"
        assert (session_dir / session_hooks.LANE0_SENTINEL).exists()

        def no_switcher(self, *a, **kw):
            raise AssertionError("the borrowed-token check built a switcher")

        monkeypatch.setattr(ClaudeAccountSwitcher, "__init__", no_switcher)
        monkeypatch.setattr("subprocess.run", no_switcher)
        with patch("claude_swap.session_hooks.write_session_credential") as writer:
            assert session_hooks.run_ensure(env=_env(session_dir)) == "fresh"
        writer.assert_not_called()

    def test_a_keychain_only_rotation_is_caught_once_the_interval_lapses(
        self, managed_switcher, block_real_keychain
    ):
        """The reason the cheap probe cannot be trusted on its own: Claude
        Code writes a rotation to lane 0's keychain item, and cswap only
        rewrites that plaintext when one is already there, so the file the
        probe reads can name a generation the login stopped serving. The
        match is therefore believed for one interval and no longer — after
        that the keychain is asked, and the newer token is copied."""
        switcher, _registry, _entry, session_dir = _register(
            managed_switcher, LANE0, source=SOURCE_LANE0
        )
        _seed_usage(switcher, pct=10.0, number="1")
        assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"
        assert _token(session_dir) == "at-live-1"

        # Rotated where the probe cannot see it: the plaintext still holds
        # at-live-1, so the fingerprint goes on matching.
        block_real_keychain.data[
            (CLAUDE_CODE_KEYCHAIN_SERVICE, macos_keychain.keychain_account_name())
        ] = json.dumps({"claudeAiOauth": {
            "accessToken": "at-live-2",
            "expiresAt": int(time.time() * 1000) + 6 * 3600 * 1000,
        }})
        assert session_hooks.run_ensure(env=_env(session_dir)) == "fresh"
        assert _token(session_dir) == "at-live-1"

        _age_sentinel(session_dir)
        assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"
        assert _token(session_dir) == "at-live-2"

    def test_a_session_that_has_not_asked_lane_0_lately_asks_again(
        self, managed_switcher
    ):
        """A sentinel that is missing (or unreadable, or stamped in a future
        the clock has since left) means "too long ago", which opens the gate
        — and the resolve past it stamps a new deadline whether it had to
        copy anything or not."""
        switcher, _registry, _entry, session_dir = _register(
            managed_switcher, LANE0, source=SOURCE_LANE0
        )
        _seed_usage(switcher, pct=10.0, number="1")
        assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"
        (session_dir / session_hooks.LANE0_SENTINEL).unlink()

        assert session_hooks.run_ensure(env=_env(session_dir)) == "fresh"
        assert (session_dir / session_hooks.LANE0_SENTINEL).exists()

    def test_a_deadline_in_the_future_is_not_a_deadline(self, managed_switcher):
        """A clock that went backwards — a laptop waking, an NTP step —
        leaves a stamp this session could otherwise sit behind for as long
        as the skew lasts. An age it cannot make sense of means "too long
        ago", and the next pass stamps a time that exists."""
        switcher, _registry, _entry, session_dir = _register(
            managed_switcher, LANE0, source=SOURCE_LANE0
        )
        _seed_usage(switcher, pct=10.0, number="1")
        assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"
        _age_sentinel(session_dir, by=-3600.0)

        assert session_hooks.run_ensure(env=_env(session_dir)) == "fresh"
        sentinel = session_dir / session_hooks.LANE0_SENTINEL
        assert sentinel.stat().st_mtime <= time.time() + 1.0

    def test_a_probe_that_opened_the_gate_for_nothing_costs_no_write(
        self, managed_switcher, block_real_keychain
    ):
        """Whatever sends this session past the gate, the authoritative
        resolve may well find nothing to do — and then it must not rewrite a
        credential the session already holds."""
        switcher, _registry, _entry, session_dir = _register(
            managed_switcher, LANE0, source=SOURCE_LANE0
        )
        _seed_usage(switcher, pct=10.0, number="1")
        assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"

        active = switcher.home / ".claude" / ".credentials.json"
        block_real_keychain.data[
            (CLAUDE_CODE_KEYCHAIN_SERVICE, macos_keychain.keychain_account_name())
        ] = active.read_text()
        active.unlink()
        _age_sentinel(session_dir)
        with patch("claude_swap.session_hooks.write_session_credential") as writer:
            assert session_hooks.run_ensure(env=_env(session_dir)) == "fresh"
        writer.assert_not_called()

    def test_a_keychain_only_login_is_not_a_reason_to_look_again(
        self, managed_switcher, block_real_keychain, monkeypatch
    ):
        """A login that keeps its credential in the keychain alone has no
        plaintext at all, and nothing here creates one — so reading "no
        file" as "it changed" would put every prompt of such a session on
        the slow path forever. It knows nothing, and the deadline decides."""
        switcher, _registry, _entry, session_dir = _register(
            managed_switcher, LANE0, source=SOURCE_LANE0
        )
        _seed_usage(switcher, pct=10.0, number="1")
        active = switcher.home / ".claude" / ".credentials.json"
        block_real_keychain.data[
            (CLAUDE_CODE_KEYCHAIN_SERVICE, macos_keychain.keychain_account_name())
        ] = active.read_text()
        active.unlink()
        assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"

        def no_switcher(self, *a, **kw):
            raise AssertionError("a fileless login sent the prompt down the slow path")

        monkeypatch.setattr(ClaudeAccountSwitcher, "__init__", no_switcher)
        monkeypatch.setattr("subprocess.run", no_switcher)
        assert session_hooks.run_ensure(env=_env(session_dir)) == "fresh"

    def test_a_lane_0_credential_that_cannot_be_read_waits_for_the_deadline(
        self, managed_switcher
    ):
        """"Unreadable" is not "changed": it says nothing, so the sentinel
        goes on holding the gate until it lapses — and then the
        authoritative resolve has nothing to copy and says so."""
        switcher, _registry, _entry, session_dir = _register(
            managed_switcher, LANE0, source=SOURCE_LANE0
        )
        _seed_usage(switcher, pct=10.0, number="1")
        assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"
        (switcher.home / ".claude" / ".credentials.json").unlink()

        assert session_hooks.run_ensure(env=_env(session_dir)) == "fresh"
        _age_sentinel(session_dir)
        assert session_hooks.run_ensure(env=_env(session_dir)) == (
            "refresh-failed:unavailable"
        )

    def test_the_holder_is_recorded_when_only_the_holder_has_changed(
        self, managed_switcher
    ):
        """A row saying `lane0` for an account that is no longer the default
        login makes every later prompt look for a rotation that cannot
        happen. Nothing is written when the token already matches, so the
        holder is what gets recorded."""
        switcher, registry, entry, session_dir = _register(
            managed_switcher, B, expires_in_ms=60_000
        )
        _seed_usage(switcher, pct=10.0)
        assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"
        registry.update(entry.session_id, source=SOURCE_LANE0)

        assert session_hooks.run_ensure(env=_env(session_dir)) == "fresh"
        assert registry.get(entry.session_id).source == "backup"

    def test_a_slot_the_cheap_read_missed_is_looked_up_again(
        self, managed_switcher, monkeypatch
    ):
        """The cheap roster read answers None for "no such account" and for
        "could not read the file" alike, so its None is never forwarded as a
        known answer. Only the switcher's guarded reader can tell those
        apart, and the difference is `transient` (retry next prompt) against
        `account-gone` (nothing to retry)."""
        switcher, _registry, _entry, session_dir = _register(
            managed_switcher, B, expires_in_ms=60_000
        )
        _seed_usage(switcher, pct=10.0)
        monkeypatch.setattr(session_hooks, "_account_slot", lambda *_a: None)
        assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"
        assert _token(session_dir) == "at-2"

    def test_a_registry_that_cannot_record_the_refresh_still_allows_a_move(
        self, managed_switcher, monkeypatch
    ):
        """Recording the new token is the least important half. A lock
        timeout there must not cancel the move the same prompt was about to
        make — that is the half a session on a spent account needs."""
        _switcher, _registry, entry, session_dir = _register(
            managed_switcher, B, expires_in_ms=60_000
        )
        _serve_usage(monkeypatch, pct=100.0, only="2")
        real = ManagedSessionRegistry.update
        seen: list = []

        def flaky(self, session_id, **changes):
            seen.append(changes)
            if len(seen) == 1:
                raise LockError("registry lock timeout")
            return real(self, session_id, **changes)

        monkeypatch.setattr(ManagedSessionRegistry, "update", flaky)
        with patch(
            "claude_swap.session_reassign.apply_reassignment",
            side_effect=lambda *a, **k: _moved(a[2], a[3]),
        ) as mover:
            assert session_hooks.run_ensure(env=_env(session_dir)).startswith(
                "reassigned:at-limit"
            )
        assert mover.called
        assert _token(session_dir) == "at-2"


class TestDaemonAndHookOnOneSession:
    """The two movers, racing over one session.

    A managed session has exactly two things that can move it: the engine's
    tick and its own prompt hook. They rank the same roster with the same
    functions, so they usually agree, and both write the same registry row.
    Every stale-row hazard in this design lives in that interleaving, so
    these drive the real hook against a real engine-side move rather than a
    stand-in for either.
    """

    C = AccountRef("c@example.com", "org-3")

    @staticmethod
    def _daemon_decision(switcher, target):
        from claude_swap.balance import AccountScore
        from claude_swap.managed_launch import Placement
        from claude_swap.session_reassign import REASON_AT_LIMIT, ReassignDecision

        score = AccountScore(
            account="3", eligible=True, score=0.0, slack=100.0,
            projected_5h=0.0, headroom=100.0, recovery_ts=0.0, reason="",
        )
        return ReassignDecision(REASON_AT_LIMIT, Placement("3", target, "backup", score))

    def test_the_daemons_plan_loses_to_a_move_the_hook_already_made(
        self, session, monkeypatch
    ):
        """The engine reads its rows at the top of a tick and plans from
        them; the hook inside the session moves it in between. Applying the
        older plan would overwrite a committed move with a decision about an
        account the session has already left."""
        from claude_swap.session_reassign import apply_reassignment

        switcher, registry, entry, session_dir = session
        stale = entry  # what the daemon's sweep handed its pass
        _serve_usage(monkeypatch, pct=100.0, only="2")
        assert session_hooks.run_ensure(env=_env(session_dir)).startswith(
            "reassigned:"
        )
        moved_to = registry.get(entry.session_id).account
        assert moved_to != stale.account
        held = _token(session_dir)

        result = apply_reassignment(
            switcher, registry, stale,
            self._daemon_decision(switcher, self.C),
            now_ms=time.time() * 1000.0, buffer_ms=0,
        )

        assert not result.ok and result.detail == "registry-changed"
        assert registry.get(entry.session_id).account == moved_to
        assert _token(session_dir) == held

    def test_a_failed_daemon_move_does_not_undo_one_the_hook_landed(
        self, session, monkeypatch
    ):
        """The daemon repointed the row and its write then failed -- while
        the hook, finding the new account at its limit too, moved the
        session on and landed a token for it. The daemon's rollback must
        take back only its own half-written move: putting the session back
        on the account of a plan two moves old would leave the row naming
        one account and the profile holding another's token."""
        from claude_swap.session_reassign import apply_reassignment

        switcher, registry, entry, session_dir = session
        stale = entry
        _serve_usage(monkeypatch, pct=100.0, only="2")

        def hook_moves_while_this_write_fails(*_a, **_kw):
            # The row names the daemon's target by now, and that account
            # turns out to be at its limit as well.
            _serve_usage(monkeypatch, pct=100.0, only="3")
            assert session_hooks.run_ensure(env=_env(session_dir)).startswith(
                "reassigned:"
            )
            return WriteResult(False, "lock-timeout")

        result = apply_reassignment(
            switcher, registry, stale,
            self._daemon_decision(switcher, self.C),
            now_ms=time.time() * 1000.0, buffer_ms=0,
            writer=hook_moves_while_this_write_fails,
        )

        assert not result.ok
        row = registry.get(entry.session_id)
        # Whatever the hook chose, the row is the hook's: a rollback clears
        # the fingerprint and writes its own `-failed` reason, and neither
        # is here.
        assert row.account != self.C
        assert row.access_fingerprint is not None
        assert not row.last_reason.endswith("-failed")


class TestEnsureMovePassInterval:
    """How often the expensive half is allowed to run.

    An at-limit account opens the gate on every prompt, and when every
    account is at its limit the move pass reaches the same answer every
    time — after a fleet usage read and a `ps` probe of every candidate
    account's profile.
    """

    def _count_passes(self, monkeypatch, outcome=None):
        calls = []

        def spy(*a, **kw):
            calls.append(kw.get("headroom"))
            return outcome

        monkeypatch.setattr(session_hooks, "_maybe_reassign", spy)
        return calls

    def test_a_second_prompt_on_the_same_reading_does_not_re_rank(
        self, session, monkeypatch
    ):
        _switcher, _registry, _entry, session_dir = session
        _serve_usage(monkeypatch, pct=100.0, only="2")
        calls = self._count_passes(monkeypatch)
        assert session_hooks.run_ensure(env=_env(session_dir)) == "fresh"
        assert session_hooks.run_ensure(env=_env(session_dir)) == "fresh"
        assert len(calls) == 1

    def test_a_changed_reading_re_ranks_inside_the_interval(
        self, session, monkeypatch
    ):
        """The skip is not a timer alone: a usage refetch that moved the
        account's headroom is exactly when the answer can differ."""
        switcher, _registry, _entry, session_dir = session
        _serve_usage(monkeypatch, pct=100.0, only="2")
        _seed_usage(switcher, pct=99.0)
        calls = self._count_passes(monkeypatch)
        session_hooks.run_ensure(env=_env(session_dir))
        _seed_usage(switcher, pct=100.0)
        session_hooks.run_ensure(env=_env(session_dir))
        assert len(calls) == 2

    def test_the_interval_runs_out_and_the_pass_runs_again(
        self, session, monkeypatch
    ):
        """The refetch that would move the reading lives inside the pass, so
        a skip that ended only on a changed reading would never end."""
        _switcher, _registry, _entry, session_dir = session
        _serve_usage(monkeypatch, pct=100.0, only="2")
        calls = self._count_passes(monkeypatch)
        session_hooks.run_ensure(env=_env(session_dir))
        later = time.time() + MOVE_RECHECK_S + 1.0
        session_hooks.run_ensure(env=_env(session_dir), clock=lambda: later)
        assert len(calls) == 2

    def test_a_pass_that_moved_the_session_stamps_nothing(
        self, session, monkeypatch
    ):
        """The sentinel records a decision to stay. A session that moved is
        on another account, with another account's headroom, and the next
        prompt has to judge it for itself."""
        _switcher, _registry, _entry, session_dir = session
        _serve_usage(monkeypatch, pct=100.0, only="2")
        calls = self._count_passes(monkeypatch, outcome="reassigned:at-limit")
        session_hooks.run_ensure(env=_env(session_dir))
        session_hooks.run_ensure(env=_env(session_dir))
        assert len(calls) == 2
        assert not (session_dir / session_hooks.MOVE_SENTINEL).exists()


class TestLane0Probe:
    """The cheap read has three answers, and only one of them is proof."""

    def _entry(self, switcher, fingerprint):
        registry = ManagedSessionRegistry(switcher.backup_dir)
        entry = registry.allocate(
            "auto-bbbbbbbb", lambda _busy: (LANE0, SOURCE_LANE0),
            pid=os.getpid(), proc_start=None,
        )
        return replace(entry, access_fingerprint=fingerprint)

    def _write_lane0(self, switcher, payload) -> None:
        (switcher.home / ".claude" / ".credentials.json").write_text(payload)

    def test_the_same_token_reads_as_unchanged(self, managed_switcher):
        payload = json.dumps({"claudeAiOauth": {"accessToken": "at-live-1"}})
        self._write_lane0(managed_switcher, payload)
        entry = self._entry(managed_switcher, oauth.access_token_fingerprint(payload))
        assert session_hooks._lane0_probe(entry) == "unchanged"

    def test_a_different_token_reads_as_changed(self, managed_switcher):
        self._write_lane0(
            managed_switcher, json.dumps({"claudeAiOauth": {"accessToken": "at-new"}})
        )
        entry = self._entry(managed_switcher, "sha256-at:whatever-it-held-before")
        assert session_hooks._lane0_probe(entry) == "changed"

    def test_no_file_reads_as_unknown(self, managed_switcher):
        (managed_switcher.home / ".claude" / ".credentials.json").unlink()
        entry = self._entry(managed_switcher, "sha256-at:something")
        assert session_hooks._lane0_probe(entry) == "unknown"

    def test_a_file_with_no_token_in_it_reads_as_unknown(self, managed_switcher):
        self._write_lane0(managed_switcher, json.dumps({"claudeAiOauth": {}}))
        entry = self._entry(managed_switcher, "sha256-at:something")
        assert session_hooks._lane0_probe(entry) == "unknown"

    def test_a_row_with_nothing_recorded_reads_as_unknown(self, managed_switcher):
        assert session_hooks._lane0_probe(self._entry(managed_switcher, None)) == (
            "unknown"
        )


class TestUrgentRefetch:
    """Whether the hook spends a network call on this account's usage."""

    def test_comfortable_headroom_is_never_refetched(self):
        assert not session_hooks._urgent_refetch_due(40.0, 600.0, threshold=90.0)

    def test_headroom_inside_the_threshold_is_refetched_once_an_interval(self):
        assert session_hooks._urgent_refetch_due(5.0, 600.0, threshold=90.0)
        assert not session_hooks._urgent_refetch_due(5.0, 5.0, threshold=90.0)

    def test_unknown_headroom_is_refetched(self):
        """An account hosting a live session whose usage nobody could read
        is precisely when a fetch is wanted, so there is no upper bound on
        staleness here — only the interval that paces the call."""
        assert session_hooks._urgent_refetch_due(None, 6000.0, threshold=90.0)
        assert session_hooks._urgent_refetch_due(None, None, threshold=90.0)
        assert not session_hooks._urgent_refetch_due(None, 5.0, threshold=90.0)


class TestEnsureReassign:
    def test_the_move_pass_works_from_the_row_the_refresh_left(
        self, managed_switcher, monkeypatch
    ):
        """A move that fails is rolled back from the entry it was handed, so
        handing it the one this pass started with would undo what the refresh
        had just corrected — and put the next prompt back in the same loop."""
        switcher, registry, entry, session_dir = _register(
            managed_switcher, B, expires_in_ms=60_000
        )
        _seed_usage(switcher, pct=10.0)
        assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"
        registry.update(entry.session_id, source=SOURCE_LANE0)
        _seed_usage(switcher, pct=100.0)
        _serve_usage(monkeypatch, pct=100.0, only="2")

        with patch(
            "claude_swap.session_reassign.apply_reassignment",
            side_effect=lambda *a, **k: _moved(a[2], a[3]),
        ) as mover:
            assert session_hooks.run_ensure(env=_env(session_dir)).startswith(
                "reassigned:at-limit"
            )
        assert mover.call_args[0][2].source == "backup"

    def test_a_row_swept_during_the_refresh_is_not_moved(
        self, managed_switcher, monkeypatch
    ):
        """The sweep can drop this row while the pass is still running — a
        recycled pid, a reset registry. What it dropped is not this hook's to
        move, and the refresh it already did still stands."""
        switcher, registry, entry, session_dir = _register(
            managed_switcher, B, expires_in_ms=60_000
        )
        _serve_usage(monkeypatch, pct=100.0, only="2")
        real = session_hooks._refresh

        def refresh_then_swept(*a, **kw):
            outcome = real(*a, **kw)
            registry.remove(entry.session_id)
            return outcome

        monkeypatch.setattr(session_hooks, "_refresh", refresh_then_swept)
        with patch(
            "claude_swap.session_reassign.apply_reassignment",
            side_effect=lambda *a, **k: _moved(a[2], a[3]),
        ) as mover:
            assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"
        mover.assert_not_called()

    def test_a_session_on_a_quarantined_account_is_moved(
        self, session, monkeypatch
    ):
        """The hook and the engine apply one policy, so a pass that gets
        there first moves the session for the same reason the tick would."""
        _switcher, _registry, _entry, session_dir = session
        # At its limit as well, so both rules apply at once. The reason is
        # what says which of them decided.
        _serve_usage(monkeypatch, pct=100.0, only="2")
        monkeypatch.setattr(
            autoswitch, "quarantined_numbers", lambda _backup_dir: {"2"}
        )
        with patch(
            "claude_swap.session_reassign.apply_reassignment",
            side_effect=lambda *a, **k: _moved(a[2], a[3]),
        ) as mover:
            assert session_hooks.run_ensure(env=_env(session_dir)) == (
                "reassigned:quarantined"
            )
        assert mover.call_args[0][3].reason == "quarantined"

    def test_a_quarantined_account_with_room_left_is_moved_as_well(
        self, session, monkeypatch
    ):
        """Quarantine is not a usage number. The engine stopped using the
        slot because working with it kept failing, and nothing refreshes it
        while it stays that way -- so a session parked there holds a token
        that still works, over quota it will never get to spend, and runs
        out of the first rather than the second. Headroom alone therefore
        cannot be what decides whether this pass so much as looks.

        The document outlives the daemon that wrote it, so there is no tick
        waiting to do this instead.
        """
        switcher, _registry, _entry, session_dir = session
        # Genuinely comfortable: a stored reading the gate can believe, not
        # the unknown headroom that opens the gate on its own.
        _seed_usage(switcher, pct=10.0)
        _serve_usage(monkeypatch, pct=1.0)
        monkeypatch.setattr(
            autoswitch, "quarantined_numbers", lambda _backup_dir: {"2"}
        )
        with patch(
            "claude_swap.session_reassign.apply_reassignment",
            side_effect=lambda *a, **k: _moved(a[2], a[3]),
        ) as mover:
            assert session_hooks.run_ensure(env=_env(session_dir)) == (
                "reassigned:quarantined"
            )
        assert mover.call_args[0][3].reason == "quarantined"

    def test_an_at_limit_session_is_moved(self, session, monkeypatch):
        _switcher, _registry, _entry, session_dir = session
        _serve_usage(monkeypatch, pct=100.0, only="2")
        with patch(
            "claude_swap.session_reassign.apply_reassignment",
            side_effect=lambda *a, **k: _moved(a[2], a[3]),
        ) as mover:
            assert session_hooks.run_ensure(env=_env(session_dir)).startswith(
                "reassigned:at-limit"
            )
        assert mover.called

    def test_a_near_limit_account_is_refetched(self, session, monkeypatch):
        switcher, _registry, _entry, session_dir = session
        _seed_usage(switcher, pct=95.0, age_s=120.0)
        asked = _serve_usage(monkeypatch, pct=95.0, age_s=120.0)
        session_hooks.run_ensure(env=_env(session_dir))
        assert asked == [{"2"}]

    def test_fresh_usage_is_not_refetched(self, session, monkeypatch):
        switcher, _registry, _entry, session_dir = session
        _seed_usage(switcher, pct=95.0, age_s=5.0)
        asked = _serve_usage(monkeypatch, pct=95.0, age_s=5.0)
        session_hooks.run_ensure(env=_env(session_dir))
        assert asked == [set()]

    def test_the_slow_path_installs_the_native_tls_verifier(
        self, session, monkeypatch
    ):
        """`cswap session` is dispatched before main() installs it, and the
        slow path is the one that can open a connection -- so it has to do
        the install itself or refresh on a machine with a corporate root
        fails with nobody watching."""
        switcher, _registry, _entry, session_dir = session
        _seed_usage(switcher, pct=95.0, age_s=120.0)
        _serve_usage(monkeypatch, pct=95.0, age_s=120.0)
        with patch("claude_swap.tls.use_native_tls") as tls:
            session_hooks.run_ensure(env=_env(session_dir))
        assert tls.call_count == 1

    def test_a_healthy_idle_session_is_refreshed_but_never_considered(
        self, managed_switcher, monkeypatch
    ):
        """The gate opens for the token, not for a move. An account with
        room is not a move candidate under any rule this hook applies, so
        the fleet usage read and the `ps` candidate probes behind it are
        skipped even though this session has been idle for hours."""
        switcher, _registry, _entry, session_dir = _register(
            managed_switcher, B, expires_in_ms=60_000,
            status="idle", idle_minutes=180.0,
        )
        _seed_usage(switcher, pct=10.0)
        asked = _serve_usage(monkeypatch, pct=10.0)
        with patch("claude_swap.session_reassign.apply_reassignment") as mover:
            assert session_hooks.run_ensure(env=_env(session_dir)) == "refreshed"
        assert asked == []
        mover.assert_not_called()

    def test_an_idle_move_is_refused_even_once_the_gate_is_open(
        self, managed_switcher, monkeypatch
    ):
        """Evening out the weekly load is the engine's job on its own
        cadence. `ensure` runs on UserPromptSubmit — the exact moment this
        session stops being idle — so an idle reading must not move it,
        however open the gate already is."""
        switcher, _registry, _entry, session_dir = _register(
            managed_switcher, B, status="idle", idle_minutes=180.0
        )
        # Busy enough that the gate opens and the balance policy prefers
        # another account, but not at its limit: the idle rule's territory.
        _seed_usage(switcher, pct=95.0, age_s=5.0)
        _serve_usage(monkeypatch, pct=95.0, age_s=5.0, only="2")
        with patch("claude_swap.session_reassign.apply_reassignment") as mover:
            assert session_hooks.run_ensure(env=_env(session_dir)) == "fresh"
        mover.assert_not_called()

    def test_a_healthy_account_never_reaches_the_move_pass(self, session, monkeypatch):
        switcher, _registry, _entry, session_dir = session
        _seed_usage(switcher, pct=10.0, age_s=5.0)
        asked = _serve_usage(monkeypatch, pct=10.0, age_s=5.0)
        assert session_hooks.run_ensure(env=_env(session_dir)) == "fresh"
        assert asked == []


@pytest.fixture
def unconfigured_logger(monkeypatch):
    """cswap's logger as a prompt hook finds it: nothing has set it up.

    The common prompt builds no switcher, and a switcher's constructor is
    the only other thing on this path that configures logging — so a hook
    that relied on somebody else having done it would write nowhere.
    """
    logger = logging.getLogger("claude-swap")
    monkeypatch.setattr(logger, "handlers", [])
    # The hook silences this logger outright when its own setup failed, and
    # that is process-global state a test must hand back as it found it.
    monkeypatch.setattr(logger, "propagate", logger.propagate)
    return logger


def _log_text() -> str:
    return (paths.get_backup_root() / "claude-swap.log").read_text(encoding="utf-8")


class TestEnsureFailsOpen:
    def test_any_exception_still_exits_zero(self, capsys):
        with patch(
            "claude_swap.session_hooks.run_ensure", side_effect=RuntimeError("boom")
        ):
            assert session_hooks.ensure() == 0
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""

    def test_a_keyboard_interrupt_still_exits_zero(self):
        with patch(
            "claude_swap.session_hooks.run_ensure", side_effect=KeyboardInterrupt
        ):
            assert session_hooks.ensure() == 0

    def test_an_outcome_never_reaches_the_prompt(self, capsys):
        """Whatever the hook did, stdout is the prompt: a word printed here
        is a word Claude reads as if the user had typed it."""
        with patch(
            "claude_swap.session_hooks.run_ensure",
            return_value="reassigned:at-limit",
        ):
            assert session_hooks.ensure() == 0
        out = capsys.readouterr()
        assert "reassigned" not in out.out + out.err
        assert out.out == "" and out.err == ""

    def test_a_swallowed_failure_is_written_to_the_log(self, unconfigured_logger):
        """Silent on the terminal is not the same as invisible. A hook that
        fails on every prompt for a week is exactly the one whose failures
        have to be findable afterwards."""
        with patch(
            "claude_swap.session_hooks.run_ensure", side_effect=RuntimeError("boom")
        ):
            assert session_hooks.ensure() == 0
        assert "RuntimeError: boom" in _log_text()

    def test_the_outcome_is_written_to_the_log(self, unconfigured_logger):
        with patch(
            "claude_swap.session_hooks.run_ensure",
            return_value="reassigned:at-limit",
        ):
            assert session_hooks.ensure() == 0
        assert "session ensure: reassigned:at-limit" in _log_text()

    def test_a_pass_that_changed_nothing_does_not_touch_the_log(
        self, unconfigured_logger
    ):
        """The healthy prompt is the common case, several a minute across a
        fleet of sessions, and it shares a 1 MB x 3 rotating log with the
        engine. A line per prompt would push the engine's own history out of
        it, so it is DEBUG — and the handler opens the file lazily, so at the
        default level it is not even created."""
        with patch("claude_swap.session_hooks.run_ensure", return_value="fresh"):
            assert session_hooks.ensure() == 0
        assert not (paths.get_backup_root() / "claude-swap.log").exists()

    def test_a_logging_setup_that_fails_still_says_nothing_on_stderr(
        self, unconfigured_logger, capsys, monkeypatch
    ):
        """If configuring the log is itself what failed, the warning about it
        has nowhere to go — and `logging.lastResort` would put it on stderr,
        which is the one thing a UserPromptSubmit hook must never do."""
        monkeypatch.setattr(logging.getLogger(), "handlers", [])
        monkeypatch.setattr(
            session_hooks, "setup_logging",
            lambda *_a, **_kw: (_ for _ in ()).throw(OSError("read-only fs")),
        )
        assert session_hooks.ensure() == 0
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


# A child that answers the one question and says how long it took. It runs
# in a process of its own because the pipe it is asked over is the point:
# a version of the check that waits for EOF would hang whatever asks it.
_PIPE_PROBE = """
import json, sys, time
from claude_swap import session_hooks
sys.stdout.write("ready\\n")
sys.stdout.flush()
start = time.monotonic()
answer = session_hooks._session_end_reason()
sys.stdout.write(
    json.dumps({"answer": answer, "elapsed": time.monotonic() - start}) + "\\n"
)
sys.stdout.flush()
"""
_PROBE_TIMEOUT_S = 15.0
# Long enough that the child has taken the first chunk before the second is
# written — the point of the split — and short enough to leave the rest of
# the budget for it.
_PROBE_PAUSE_S = 0.25


def _ask_over_a_pipe(chunks, *, pause=0.0, close=False):
    """Ask the stdin check over a pipe whose write end is never closed.

    That is the shape a wrapper script, a fifo or a shell holding the write
    end open in a sibling gives a hook, and the one where "read to EOF"
    means "read forever". The child says when it is about to ask, so a
    chunk written after that is one the read has to wait for rather than
    one already sitting in the pipe. It is given a hard timeout and is
    reaped either way, so a check that blocks fails this test rather than
    hanging the suite.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _PIPE_PROBE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        # Bounded like everything else here: an import that fails closes
        # stdout and answers at once, but one that HANGS would otherwise
        # hang the suite before the timeout below ever applied.
        if not select.select([proc.stdout], [], [], _PROBE_TIMEOUT_S)[0]:
            pytest.fail("the probe never got as far as asking")
        assert proc.stdout.readline().strip() == "ready"
        for index, chunk in enumerate(chunks):
            if index:
                time.sleep(pause)
            proc.stdin.write(chunk)
            proc.stdin.flush()
        if close:
            proc.stdin.close()
        try:
            proc.wait(timeout=_PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            pytest.fail("the stdin read never came back: it waits for an EOF")
        return json.loads(proc.stdout.readline())
    finally:
        proc.kill()
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except OSError:  # the child is gone; nothing left to flush into
                pass
        proc.wait()


@pytest.fixture
def session_end(monkeypatch):
    """stdin as Claude hands it to a hook for a session that is ending.

    A live row is only released to the process that owns it AND can show
    this, so every test standing in for a real SessionEnd has to say so —
    with a reason that means the process is going away. `prompt_input_exit`
    is the one an ordinary Ctrl-D or /exit produces; `clear` and `resume`
    are SessionEnds too and deliberately do NOT release (see
    TestReleaseReasons).
    """
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "session_id": "s-1",
        "hook_event_name": "SessionEnd",
        "reason": "prompt_input_exit",
    })))


def _item(session_dir):
    from claude_swap.session import _keychain_account_name, keychain_service_name

    return keychain_service_name(session_dir), _keychain_account_name()


class TestRelease:
    """`cswap session release`: handing the account back at SessionEnd."""

    def test_it_removes_the_row_and_the_profile(self, session, session_end):
        _switcher, registry, entry, session_dir = session
        assert session_hooks.run_release(env=_env(session_dir)) == "released"
        assert registry.get(entry.session_id) is None
        assert not session_dir.exists()

    def test_it_deletes_the_keychain_item(
        self, session, session_end, block_real_keychain
    ):
        _switcher, _registry, _entry, session_dir = session
        macos_keychain.set_password(*_item(session_dir), "secret")
        assert session_hooks.run_release(env=_env(session_dir)) == "released"
        assert macos_keychain.get_password(*_item(session_dir)) is None

    def test_an_unmanaged_config_dir_is_a_no_op(self, managed_switcher, tmp_path):
        assert session_hooks.run_release(
            env={"CLAUDE_CONFIG_DIR": str(tmp_path)}
        ) == "not-managed"
        assert tmp_path.exists()

    def test_a_row_already_gone_leaves_the_directory_to_the_sweep(self, session):
        """Nothing to hand back, and nothing here knows whether the directory
        is still being read — which is the orphan pass's question, under its
        own grace period."""
        _switcher, registry, entry, session_dir = session
        registry.remove(entry.session_id)
        assert session_hooks.run_release(env=_env(session_dir)) == "no-entry"
        assert session_dir.exists()

    def test_a_registry_nobody_can_read_releases_nothing(self, session):
        """"Cannot read" is folded into "no such row" by the reader every
        poller uses, and two deletions hang off that answer here. Asked the
        explicit way instead, and refused."""
        _switcher, _registry, entry, session_dir = session
        (session_dir.parent / "managed.json").write_text("{ not json")
        assert session_hooks.run_release(env=_env(session_dir)) == (
            "registry-unreadable"
        )
        assert session_dir.exists()

    def test_a_session_still_running_under_somebody_else_is_left_alone(
        self, managed_switcher
    ):
        """The reservation exists before the profile does and a Claude writes
        its own record only once it is up, so a live row whose process is not
        the one asking must keep both its row and its directory — this is the
        window a profile used to be removed in."""
        child = subprocess.Popen(["sleep", "30"])
        try:
            _switcher, registry, entry, session_dir = _register(
                managed_switcher, B, pid=child.pid
            )
            assert session_hooks.run_release(env=_env(session_dir)) == "still-running"
            assert registry.get(entry.session_id) is not None
            assert session_dir.exists()
        finally:
            child.kill()
            child.wait()

    def test_a_row_whose_process_has_gone_is_released_by_anyone(
        self, managed_switcher
    ):
        """The other half of the same rule: a session that has already ended
        does not have to prove who is asking — no payload on stdin, and
        released anyway. A hook outliving the Claude that spawned it, or a
        hand-run release for a crashed one, still hands the account back."""
        child = subprocess.Popen(["true"])
        child.wait()
        if process_detection.is_pid_alive(child.pid):
            pytest.skip("pid recycled between wait() and the check")
        _switcher, registry, entry, session_dir = _register(
            managed_switcher, B, pid=child.pid
        )
        assert session_hooks.run_release(env=_env(session_dir)) == "released"
        assert registry.get(entry.session_id) is None
        assert not session_dir.exists()

    def test_it_looks_past_the_shell_that_ran_the_hook(self):
        """Claude runs a hook through a shell, so the process asking is a
        grandchild of the session it belongs to as often as a child. Asked
        from one generation further down, with a real shell in between."""
        script = (
            "from claude_swap.session_hooks import _this_session_is;"
            f"print(_this_session_is({os.getpid()}))"
        )
        # `; true` keeps the shell from exec'ing python into its own pid,
        # which is what puts a generation between this process and the ask.
        proc = subprocess.run(
            ["sh", "-c", f"{sys.executable} -c '{script}'; true"],
            capture_output=True, text=True, timeout=120,
        )
        assert proc.stdout.strip() == "True", proc.stderr

    def test_a_profile_another_claude_still_uses_keeps_its_directory(
        self, session, session_end
    ):
        _switcher, registry, entry, session_dir = session
        other = 4243
        (session_dir / "sessions" / f"{other}.json").write_text(json.dumps({
            "pid": other, "status": "busy", "cwd": "/work",
        }))
        with (
            patch("claude_swap.process_detection.is_pid_alive", return_value=True),
            patch(
                "claude_swap.process_detection.pid_matches_record", return_value=True
            ),
        ):
            assert session_hooks.run_release(env=_env(session_dir)) == "left-in-use"
        assert registry.get(entry.session_id) is None
        assert session_dir.exists()

    def test_a_refused_removal_leaves_the_keychain_item_alone(
        self, session, session_end, block_real_keychain
    ):
        """The item goes with the directory and never before it: a profile
        kept because something is still reading it would otherwise keep a
        credential file the item no longer shadows, which is the one state
        the writer's degrade exists to avoid creating."""
        _switcher, _registry, entry, session_dir = session
        macos_keychain.set_password(*_item(session_dir), "secret")
        other = 4243
        (session_dir / "sessions" / f"{other}.json").write_text(json.dumps({
            "pid": other, "status": "busy", "cwd": "/work",
        }))
        with (
            patch("claude_swap.process_detection.is_pid_alive", return_value=True),
            patch(
                "claude_swap.process_detection.pid_matches_record", return_value=True
            ),
        ):
            assert session_hooks.run_release(env=_env(session_dir)) == "left-in-use"
        assert macos_keychain.get_password(*_item(session_dir)) == "secret"

    def test_an_unreadable_record_keeps_the_directory(self, session, session_end):
        """Not knowing what is running is not knowing that nothing is."""
        _switcher, _registry, _entry, session_dir = session
        (session_dir / "sessions" / "77.json").write_text("{ not json")
        assert session_hooks.run_release(env=_env(session_dir)) == "left-in-use"
        assert session_dir.exists()

    def test_a_live_session_that_is_ours_and_ending_is_released(
        self, managed_switcher, session_end
    ):
        """The normal case, and the only one where anything live is deleted:
        the row names a process this one is running under — a generation up,
        as it is when Claude spawns the hook — and stdin says that session is
        ending. Its own record is in the profile and does not hold it, which
        is why the sweep's quiescence test cannot be used as it stands."""
        _switcher, registry, entry, session_dir = _register(
            managed_switcher, B, pid=os.getppid()
        )
        assert entry.pid != os.getpid()
        assert (session_dir / "sessions" / f"{entry.pid}.json").exists()
        assert session_hooks.run_release(env=_env(session_dir)) == "released"
        assert registry.get(entry.session_id) is None
        assert not session_dir.exists()

    @pytest.mark.parametrize("stream, text", [
        (_Tty, json.dumps({                                     # a terminal
            "hook_event_name": "SessionEnd", "reason": "logout",
        })),
        (io.StringIO, ""),                                      # nothing at all
        (io.StringIO, "not json"),
        (io.StringIO, json.dumps({"hook_event_name": "PreToolUse"})),
        (io.StringIO, json.dumps(["SessionEnd"])),              # not an object
    ])
    def test_a_live_session_is_not_released_by_anything_but_its_own_end(
        self, session, monkeypatch, block_real_keychain, stream, text
    ):
        """A Bash tool, a subshell, a person in a split pane: all of them run
        under the very ancestry the hook runs under, so ancestry alone would
        delete a running session's credential out from under it. Only the
        event Claude hands the hook on stdin tells the two apart."""
        _switcher, registry, entry, session_dir = session
        macos_keychain.set_password(*_item(session_dir), "secret")
        monkeypatch.setattr(sys, "stdin", stream(text))
        assert session_hooks.run_release(env=_env(session_dir)) == (
            "not-a-session-end"
        )
        assert registry.get(entry.session_id) is not None
        assert session_dir.exists()
        assert macos_keychain.get_password(*_item(session_dir)) == "secret"

    @pytest.mark.parametrize("chunks, answer", [
        ([json.dumps({"hook_event_name": "SessionEnd", "reason": "logout"})],
         "logout"),
        (['{"hook_event_name": "SessionEnd", "rea', 'son": "logout"}'],
         "logout"),
        (['{"hook_event_name": "SessionEnd", "rea'], None),
    ], ids=["whole", "in-pieces", "truncated"])
    def test_the_event_is_read_under_a_deadline_not_to_an_eof(
        self, chunks, answer
    ):
        """Claude writes the event and moves on; nothing says the write end
        is ever closed, and on a pipe "read to EOF" then means "read until
        the hook timeout kills me", with the session's teardown waiting on
        it. Whole, in pieces, or cut off: the answer comes back inside the
        budget, and a payload split across writes is still read as one."""
        result = _ask_over_a_pipe(chunks, pause=_PROBE_PAUSE_S)
        assert result["answer"] == answer
        assert result["elapsed"] < session_hooks._STDIN_WAIT_S * 2

    def test_a_descriptor_it_cannot_wait_on_is_not_read(self, monkeypatch):
        """The wait is what bounds the read: reading a real descriptor with
        no budget behind it is the one move that might never come back. Out
        of reach in practice — a hook's stdin is fd 0 and `select` does not
        fail on that — but the answer when it cannot be waited on is "no
        SessionEnd", not "read it anyway and hope"."""
        reads = []

        class Stream:
            class buffer:  # noqa: N801 - stands in for `sys.stdin.buffer`
                @staticmethod
                def read1(size):
                    reads.append(size)
                    return json.dumps({"hook_event_name": "SessionEnd"}).encode()

            def isatty(self):
                return False

            def fileno(self):
                return 0

        def no_select(*_args):
            raise OSError("fd out of range")

        monkeypatch.setattr(sys, "stdin", Stream())
        monkeypatch.setattr(session_hooks.select, "select", no_select)
        assert session_hooks._session_end_reason() is None
        assert reads == []

    def test_a_stream_that_never_stops_is_given_up_on(self, monkeypatch):
        """Stdin is inherited, and a release run under something writing a
        lot to it -- a build log, another tool's output -- would otherwise
        accumulate the whole of it for the budget and reparse everything
        accumulated after every chunk, which is quadratic. The event is a
        few hundred bytes; past a megabyte this stream is not it."""
        reads = []

        class Firehose:
            class buffer:  # noqa: N801 - stands in for `sys.stdin.buffer`
                @staticmethod
                def read1(size):
                    reads.append(size)
                    return b"x" * size

            def isatty(self):
                return False

            def fileno(self):
                return 0

        monkeypatch.setattr(sys, "stdin", Firehose())
        monkeypatch.setattr(
            session_hooks.select, "select", lambda *_a: ([sys.stdin], [], [])
        )
        assert session_hooks._session_end_reason() is None
        # Bounded by the cap, not by the deadline: one chunk past it is all
        # it takes, and it never went back for more.
        assert sum(reads) <= session_hooks._STDIN_MAX + session_hooks._STDIN_CHUNK

    def test_a_payload_just_under_the_cap_is_still_read(self, monkeypatch):
        """The cap is generous on purpose, so it must not be what answers a
        real event that happens to arrive behind some padding."""
        # A literal half-megabyte, not a fraction of the cap: sized against
        # the cap, this test would shrink with it and a cap tightened to a
        # kilobyte would still look right.
        assert session_hooks._STDIN_MAX == 1024 * 1024
        event = json.dumps({
            "hook_event_name": "SessionEnd",
            "reason": "logout",
            "pad": "p" * 512 * 1024,
        }).encode()
        chunks = [
            event[i:i + session_hooks._STDIN_CHUNK]
            for i in range(0, len(event), session_hooks._STDIN_CHUNK)
        ]

        class Stream:
            class buffer:  # noqa: N801 - stands in for `sys.stdin.buffer`
                @staticmethod
                def read1(_size):
                    return chunks.pop(0) if chunks else b""

            def isatty(self):
                return False

            def fileno(self):
                return 0

        monkeypatch.setattr(sys, "stdin", Stream())
        monkeypatch.setattr(
            session_hooks.select, "select", lambda *_a: ([sys.stdin], [], [])
        )
        assert session_hooks._session_end_reason() == "logout"

    def test_a_stream_that_ends_is_not_waited_out(self):
        """EOF is an answer, not a reason to sit out the rest of the budget:
        whatever was going to arrive has arrived, and a session teardown is
        waiting on this."""
        result = _ask_over_a_pipe(['{"hook_event_name": "Sess'], close=True)
        assert result["answer"] is None
        assert result["elapsed"] < session_hooks._STDIN_WAIT_S / 2

    def test_a_directory_that_would_not_go_is_reported(self, session, session_end):
        _switcher, registry, entry, session_dir = session
        with patch(
            "claude_swap.session_hooks.remove_managed_profile", return_value=False
        ):
            assert session_hooks.run_release(env=_env(session_dir)) == "left-behind"
        assert registry.get(entry.session_id) is None
        assert session_dir.exists()

    def test_it_fails_open(self, capsys):
        with patch(
            "claude_swap.session_hooks.run_release", side_effect=RuntimeError("boom")
        ):
            assert session_hooks.release() == 0
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""

    def test_what_it_did_is_written_to_the_log(self, unconfigured_logger):
        """Once per session, so a line at the default level costs nothing and
        answers "did the account ever come back?"."""
        with patch("claude_swap.session_hooks.run_release", return_value="released"):
            assert session_hooks.release() == 0
        assert "session release: released" in _log_text()

    def test_a_refusal_is_written_down_once(self, session, unconfigured_logger):
        """Each refusal already explains itself in a warning that names what
        was refused and why. Repeating the bare outcome at INFO underneath it
        would put the same story in the shared log twice."""
        _switcher, _registry, _entry, session_dir = session
        (session_dir.parent / "managed.json").write_text("{ not json")
        with patch("claude_swap.session_hooks.os.environ", _env(session_dir)):
            assert session_hooks.release() == 0
        log = _log_text()
        assert "the registry cannot be read" in log
        assert "session release: registry-unreadable" not in log

    def test_a_missing_event_reads_as_missing_not_as_a_hand_run(
        self, session, monkeypatch, unconfigured_logger
    ):
        """The same refusal covers two different stories: somebody ran the
        verb by hand inside a live session, and a real SessionEnd whose
        payload was merely slow. The warning has to leave room for the
        second, or a reader chasing a release that did not happen goes
        looking for a command nobody typed."""
        _switcher, _registry, _entry, session_dir = session
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        with patch("claude_swap.session_hooks.os.environ", _env(session_dir)):
            assert session_hooks.release() == 0
        log = _log_text()
        assert "no SessionEnd reached this hook" in log
        assert "did not arrive within" in log
        assert "Ending the session is what releases it" in log

    def test_a_pass_with_nothing_to_do_does_not_touch_the_log(
        self, unconfigured_logger
    ):
        with patch("claude_swap.session_hooks.run_release", return_value="no-entry"):
            assert session_hooks.release() == 0
        assert not (paths.get_backup_root() / "claude-swap.log").exists()


# The SessionEnd reasons Claude Code's shutdown path passes to its hook
# runner, written out rather than imported: see the test below.
_ENDING_REASONS = ("logout", "prompt_input_exit", "other")


class TestReleaseReasons:
    """Which SessionEnd reasons take a LIVE session's account away.

    Claude Code fires SessionEnd five ways. Three of them run from its
    shutdown path and the process is gone moments later; two of them —
    `/clear` and `/resume` — leave the session running with the same pid,
    the same profile and the same credential. Releasing on those would
    delete the account, the keychain item and the whole profile out from
    under somebody mid-conversation, silently, because both verbs are
    silent by design.
    """

    def _stdin(self, monkeypatch, payload):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    def test_the_allowlist_is_exactly_the_reasons_that_end_the_process(self):
        """Spelled out here rather than read off the constant: a test that
        parametrises over the set under test says nothing about what is in
        it, and shrinking the set would silently shrink the test with it."""
        assert session_hooks._ENDING_REASONS == frozenset(_ENDING_REASONS)

    @pytest.mark.parametrize("reason", _ENDING_REASONS)
    def test_a_reason_that_ends_the_process_releases(
        self, session, monkeypatch, block_real_keychain, reason
    ):
        _switcher, registry, entry, session_dir = session
        macos_keychain.set_password(*_item(session_dir), "secret")
        self._stdin(monkeypatch, {"hook_event_name": "SessionEnd", "reason": reason})
        assert session_hooks.run_release(env=_env(session_dir)) == "released"
        assert registry.get(entry.session_id) is None
        assert not session_dir.exists()
        assert macos_keychain.get_password(*_item(session_dir)) is None

    @pytest.mark.parametrize("payload, shown", [
        ({"hook_event_name": "SessionEnd", "reason": "clear"}, "clear"),
        ({"hook_event_name": "SessionEnd", "reason": "resume"}, "resume"),
        ({"hook_event_name": "SessionEnd", "reason": "hibernate"}, "hibernate"),
        ({"hook_event_name": "SessionEnd"}, "none given"),
        ({"hook_event_name": "SessionEnd", "reason": 7}, "none given"),
    ], ids=["clear", "resume", "unknown", "absent", "not-a-string"])
    def test_a_session_that_carries_on_keeps_everything(
        self, session, monkeypatch, block_real_keychain, unconfigured_logger,
        payload, shown,
    ):
        """/clear is the one that matters most: the row is live, the hook is
        that Claude's own child, and stdin really does say SessionEnd. Only
        the reason says the session is still there."""
        _switcher, registry, entry, session_dir = session
        macos_keychain.set_password(*_item(session_dir), "secret")
        self._stdin(monkeypatch, payload)
        with patch("claude_swap.session_hooks.os.environ", _env(session_dir)):
            assert session_hooks.release() == 0
        assert registry.get(entry.session_id) == entry
        assert session_dir.exists()
        assert (session_dir / ".credentials.json").exists()
        assert macos_keychain.get_password(*_item(session_dir)) == "secret"
        log = _log_text()
        assert f"SessionEnd ({shown})" in log
        assert "releasing nothing" in log

    def test_a_dead_row_is_released_whatever_the_reason_says(
        self, managed_switcher, monkeypatch
    ):
        """A process that has exited settles it on its own: the reason gate
        exists to protect a session that is still running, and there is none
        here to protect."""
        _switcher, registry, entry, session_dir = _register(
            managed_switcher, B, pid=999_999
        )
        self._stdin(monkeypatch, {"hook_event_name": "SessionEnd", "reason": "clear"})
        with patch(
            "claude_swap.process_detection.is_pid_alive", return_value=False
        ):
            assert session_hooks.run_release(env=_env(session_dir)) == "released"
        assert registry.get(entry.session_id) is None
        assert not session_dir.exists()
