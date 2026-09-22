"""The managed-session prompt hooks (Refs #382)."""

from __future__ import annotations

import json
import logging
import os
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
    status="busy", idle_minutes=0.0,
):
    """A live managed session on ``account`` whose profile holds a token."""
    registry = ManagedSessionRegistry(switcher.backup_dir)
    session_id = "auto-aaaaaaaa"
    entry = registry.allocate(
        session_id, lambda _busy: (account, source),
        pid=os.getpid(), proc_start=None,
    )
    session_dir = registry.session_dir(session_id)
    create_managed_profile(session_dir)
    # Claude's own record for this instance: without one the move rules
    # refuse, since a session Claude has not registered may still be
    # reading the credential its launch wrote.
    (session_dir / "sessions").mkdir(parents=True, exist_ok=True)
    (session_dir / "sessions" / f"{os.getpid()}.json").write_text(json.dumps({
        "pid": os.getpid(), "status": status, "cwd": "/work",
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
