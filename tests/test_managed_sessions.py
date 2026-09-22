"""Managed sessions: ids, registry, reservations (Refs #382)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from claude_swap import macos_keychain
from claude_swap import managed_sessions as ms
from claude_swap.exceptions import LockError, SessionError
from claude_swap.locking import FileLock
from claude_swap.managed_sessions import (
    AccountRef,
    ManagedSessionRegistry,
    entry_is_live,
    is_managed_session_id,
    new_session_id,
    process_stamp,
)
from claude_swap.models import Platform
from claude_swap.process_detection import process_start_ticks
from claude_swap.session import keychain_service_name

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="managed sessions are POSIX-only (v1)"
)

A = AccountRef("a@example.com", "org-a")
B = AccountRef("b@example.com", "org-b")
DEAD_PID = 999_999


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    path = tmp_path / "backup"
    path.mkdir()
    return path


@pytest.fixture
def registry(backup_dir: Path) -> ManagedSessionRegistry:
    return ManagedSessionRegistry(backup_dir)


def _always(account: AccountRef, source: str = "backup"):
    return lambda busy: (account, source)


class TestIds:
    def test_new_ids_are_managed_and_unique(self):
        ids = {new_session_id() for _ in range(50)}
        assert len(ids) == 50
        assert all(is_managed_session_id(i) for i in ids)

    @pytest.mark.parametrize(
        "name",
        ["2-b_example.com", "auto-", "auto-XYZ12345", "auto-1234567",
         "auto-123456789", "auto-0000beef.bak", "managed.json"],
    )
    def test_per_account_and_malformed_names_are_not_managed(self, name):
        assert not is_managed_session_id(name)

    def test_process_stamp_matches_own_process(self):
        """Exact identity, not just "matches" (``pid_matches_record`` also
        passes a None stamp and, off Linux, any command line containing
        "claude" - too weak to catch a broken ``process_stamp``), and
        checked against a fresh ``ps`` call rather than reusing the
        implementation's own helpers, so a bug shared between
        ``process_stamp`` and ``process_started_at``/``_lstart_seconds``
        would still be caught."""
        pid = os.getpid()
        stamp = process_stamp(pid)
        assert stamp is not None
        if sys.platform.startswith("linux"):
            assert stamp == process_start_ticks(pid)
        else:
            # Same invocation and env as process_detection._ps(pid, "lstart"):
            # -o lstart= for a bare value, LC_ALL=C TZ=UTC so the reading
            # doesn't depend on the host's locale/timezone (a German locale
            # on this very box renders lstart as "Di. 22 Sep. ..." without
            # it). Whitespace is normalized on both sides rather than only
            # the `ps` side: `ps -o lstart=` space-pads a single-digit day
            # to two characters (``_lstart_seconds``'s own docstring shows
            # "Wed Sep  2 ..."), and so does time.asctime (confirmed:
            # time.asctime for day=3 renders "Mon Sep  3 ..."), so comparing
            # only-`ps`-normalized text against a raw `stamp` would go
            # flaky for nine days out of every month.
            ps = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
                capture_output=True,
                text=True,
                timeout=5,
            )
            # Same failure treatment as process_detection._ps: a nonzero
            # exit or empty stdout means "unknowable", not "no output",
            # so trust the comparison only once ps actually answered.
            assert ps.returncode == 0
            assert ps.stdout.strip()
            assert " ".join(stamp.split()) == " ".join(ps.stdout.split())


class TestRegistry:
    def test_allocate_records_entry(self, registry):
        entry = registry.allocate(
            "auto-0000beef", _always(B), pid=os.getpid(), proc_start="stamp"
        )
        assert entry is not None
        assert (entry.account, entry.pid, entry.proc_start) == (B, os.getpid(), "stamp")
        assert (entry.source, entry.last_reason) == ("backup", "launch")
        assert entry.created_at == entry.last_assigned_at
        raw = json.loads(registry.path.read_text())
        assert raw["schemaVersion"] == 1
        assert raw["sessions"]["auto-0000beef"]["account"] == {
            "email": "b@example.com", "organizationUuid": "org-b",
        }
        assert registry.get("auto-0000beef") == entry

    def test_chooser_declining_writes_nothing(self, registry):
        assert registry.allocate(
            "auto-0000beef", lambda busy: None, pid=os.getpid(), proc_start=None
        ) is None
        assert registry.entries() == {}
        assert not registry.path.exists()

    def test_declined_allocate_leaves_root_private(self, registry):
        """``_lock`` must mkdir ``root`` privately itself: ``FileLock.acquire``
        also mkdirs the lock file's parent, but at the default (loose) mode,
        and a declined allocate never reaches ``_write`` to correct it."""
        assert registry.allocate(
            "auto-0000beef", lambda busy: None, pid=os.getpid(), proc_start=None
        ) is None
        assert registry.root.is_dir()
        assert registry.root.stat().st_mode & 0o777 == 0o700

    def test_allocate_rejects_invalid_pid_before_choosing(self, registry):
        chosen = False

        def choose(busy):
            nonlocal chosen
            chosen = True
            return (B, "backup")

        with pytest.raises(ValueError):
            registry.allocate("auto-0000beef", choose, pid=0, proc_start=None)
        assert not chosen
        assert registry.entries() == {}
        assert not registry.path.exists()

    def test_allocate_rejects_invalid_account_or_source_from_chooser(self, registry):
        with pytest.raises(ValueError):
            registry.allocate(
                "auto-0000beef", lambda busy: (AccountRef(""), "backup"),
                pid=os.getpid(), proc_start=None,
            )
        with pytest.raises(ValueError):
            registry.allocate(
                "auto-0000cafe", lambda busy: (B, 123),
                pid=os.getpid(), proc_start=None,
            )
        with pytest.raises(ValueError):
            registry.allocate(
                "auto-0000f00d",
                lambda busy: (AccountRef("c@example.com", 123), "backup"),
                pid=os.getpid(), proc_start=None,
            )
        assert registry.entries() == {}
        assert not registry.path.exists()

    def test_update_rejects_rows_that_would_not_round_trip(self, registry):
        registry.allocate("auto-0000beef", _always(B), pid=os.getpid(), proc_start=None)
        for changes in (
            {"pid": True},
            {"pid": 1},
            {"account": AccountRef("")},
            {"account": AccountRef("c@example.com", 123)},
            {"source": 123},
        ):
            with pytest.raises(ValueError):
                registry.update("auto-0000beef", **changes)
            # A rejected update must not have touched the stored entry.
            assert registry.get("auto-0000beef").pid == os.getpid()
            assert registry.get("auto-0000beef").account == B

    def test_allocate_raises_on_lock_timeout(self, registry, backup_dir):
        blocker = FileLock(registry.root / ms.REGISTRY_LOCK_FILENAME)
        assert blocker.acquire()
        try:
            impatient = ManagedSessionRegistry(backup_dir, lock_timeout=0.2)
            with pytest.raises(LockError):
                impatient.allocate(
                    "auto-0000beef", _always(B), pid=os.getpid(), proc_start=None
                )
            assert not registry.path.exists()
        finally:
            blocker.release()

    def test_get_locked_reads_under_the_lock_and_times_out(self, registry):
        entry = registry.allocate(
            "auto-0000beef", _always(B), pid=os.getpid(), proc_start=None
        )
        assert registry.get_locked("auto-0000beef") == entry
        assert registry.get_locked("auto-00000000") is None
        blocker = FileLock(registry.root / ms.REGISTRY_LOCK_FILENAME)
        assert blocker.acquire()
        try:
            started = time.monotonic()
            with pytest.raises(LockError):
                registry.get_locked("auto-0000beef", timeout=0.2)
            assert time.monotonic() - started < 5
        finally:
            blocker.release()

    def test_duplicate_or_foreign_id_is_refused(self, registry):
        registry.allocate("auto-0000beef", _always(B), pid=os.getpid(), proc_start=None)
        with pytest.raises(ValueError):
            registry.allocate("auto-0000beef", _always(A), pid=os.getpid(), proc_start=None)
        with pytest.raises(ValueError):
            registry.allocate("2-b_example.com", _always(A), pid=os.getpid(), proc_start=None)

    def test_update_and_remove(self, registry):
        registry.allocate("auto-0000beef", _always(B), pid=os.getpid(), proc_start=None)
        updated = registry.update("auto-0000beef", access_fingerprint="fp", source="lane0")
        assert (updated.access_fingerprint, updated.source) == ("fp", "lane0")
        with pytest.raises(TypeError):
            registry.update("auto-0000beef", created_at="x")
        assert registry.remove("auto-0000beef").session_id == "auto-0000beef"
        assert registry.get("auto-0000beef") is None
        assert registry.update("auto-0000beef", source="backup") is None
        assert registry.remove("auto-0000beef") is None

    def test_corrupt_registry_reads_as_empty_but_is_never_rewritten(self, registry):
        """Polling callers treat an unreadable registry as empty; a mutator
        must not. `_write` persists only what was read back, so rewriting a
        file whose rows could not all be read drops live sessions'
        reservations for good."""
        registry.path.parent.mkdir(parents=True, exist_ok=True)
        registry.path.write_text("{not json")
        assert registry.entries() == {}
        assert registry.unreadable_reason() is not None
        for mutate in (
            lambda: registry.allocate(
                "auto-0000beef", _always(B), pid=os.getpid(), proc_start=None
            ),
            lambda: registry.update("auto-0000beef", source="lane0"),
            lambda: registry.remove("auto-0000beef"),
        ):
            with pytest.raises(SessionError, match="cannot be read"):
                mutate()
        assert registry.path.read_text() == "{not json"

    @pytest.mark.parametrize(
        "document",
        [
            {"sessions": {}},                          # no schemaVersion at all
            {"schemaVersion": "1", "sessions": {}},    # not an integer
            {"schemaVersion": 2, "sessions": {}},      # newer than this build
        ],
    )
    def test_a_schema_version_this_build_cannot_model_is_refused(
        self, registry, document
    ):
        """The version field exists to stop this build rewriting a registry a
        newer one wrote: its rows are other sessions' reservations, in a
        shape `ManagedEntry.from_json` may read short. Reading short is
        harmless; writing back what was read is not."""
        registry.path.parent.mkdir(parents=True, exist_ok=True)
        registry.path.write_text(json.dumps(document))

        assert registry.entries() == {}
        assert "schemaVersion" in registry.unreadable_reason()
        with pytest.raises(SessionError, match="cannot be read"):
            registry.allocate(
                "auto-0000beef", _always(B), pid=os.getpid(), proc_start=None
            )
        assert json.loads(registry.path.read_text()) == document

    def test_a_registry_this_build_wrote_reads_back_normally(self, registry):
        """The version guard must not refuse this build's own file, and an
        absent one is still just "no sessions"."""
        assert registry.unreadable_reason() is None
        assert registry.entries() == {}
        registry.allocate("auto-0000beef", _always(B), pid=os.getpid(), proc_start=None)
        assert json.loads(registry.path.read_text())["schemaVersion"] == (
            ms.REGISTRY_SCHEMA_VERSION
        )
        assert registry.unreadable_reason() is None
        assert list(registry.entries()) == ["auto-0000beef"]

    def test_malformed_rows_are_dropped_and_named(self, registry, caplog):
        """A row that cannot be modelled is skipped, not fatal -- but the
        next mutator erases it from disk, so each dropped id is logged or
        there is nothing left to correlate the vanished reservation with."""
        registry.path.parent.mkdir(parents=True, exist_ok=True)
        registry.path.write_text(json.dumps({"schemaVersion": 1, "sessions": {
            "auto-0000beef": {"account": {"email": "b@example.com"}, "pid": os.getpid()},
            "auto-bad": {"account": {"email": "b@example.com"}, "pid": os.getpid()},
            "auto-0000cafe": {"account": {}, "pid": os.getpid()},
            "auto-0000f00d": {"account": {"email": "b@example.com"}, "pid": True},
        }}))

        with caplog.at_level("WARNING", logger="claude-swap"):
            assert list(registry.entries()) == ["auto-0000beef"]

        assert registry.get("auto-0000beef").account == AccountRef("b@example.com", "")
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "auto-bad" in logged
        assert "auto-0000cafe" in logged
        assert "auto-0000f00d" in logged
        assert "auto-0000beef" not in logged

    def test_the_existence_check_drops_rows_quietly(self, registry, caplog):
        """`has_state` is polled every tick, so its read must not log the
        same dropped row forever."""
        registry.path.parent.mkdir(parents=True, exist_ok=True)
        registry.path.write_text(json.dumps({"schemaVersion": 1, "sessions": {
            "auto-0000cafe": {"account": {}, "pid": os.getpid()},
        }}))

        with caplog.at_level("WARNING", logger="claude-swap"):
            assert registry.has_state() is False

        assert not [r for r in caplog.records if "auto-0000cafe" in r.getMessage()]

    def test_an_unstamped_reservation_is_logged(self, registry, caplog):
        """Without a start stamp, liveness degrades to bare "is the pid
        alive": a recycled pid keeps the row alive and nothing reclaims it
        until that pid dies. Safe, but it has to be visible."""
        with caplog.at_level("WARNING", logger="claude-swap"):
            registry.allocate(
                "auto-0000beef", _always(B), pid=os.getpid(), proc_start=None
            )
            registry.allocate(
                "auto-0000cafe", _always(B), pid=os.getpid(), proc_start="stamp"
            )

        logged = [r.getMessage() for r in caplog.records if "start stamp" in r.getMessage()]
        assert len(logged) == 1
        assert "auto-0000beef" in logged[0]

    def test_remove_to_empty_unlinks_registry_file(self, registry):
        """An empty registry must not keep upkeep (has_state/sweep)
        running forever: dropping the last entry removes managed.json
        rather than leaving a ``{"sessions": {}}`` husk on disk."""
        registry.allocate("auto-0000beef", _always(B), pid=os.getpid(), proc_start=None)
        assert registry.path.exists()
        registry.remove("auto-0000beef")
        assert not registry.path.exists()
        assert registry.entries() == {}

    def test_remove_keeps_file_when_entries_remain(self, registry):
        registry.allocate("auto-00000001", _always(B), pid=os.getpid(), proc_start=None)
        registry.allocate("auto-00000002", _always(A), pid=os.getpid(), proc_start=None)
        registry.remove("auto-00000001")
        assert registry.path.exists()
        assert list(registry.entries()) == ["auto-00000002"]


class TestHasState:
    def test_true_only_while_entries_exist(self, registry):
        assert registry.has_state() is False
        registry.allocate("auto-0000beef", _always(B), pid=os.getpid(), proc_start=None)
        assert registry.has_state() is True
        registry.remove("auto-0000beef")
        assert registry.has_state() is False

    def test_leftover_or_empty_file_reads_as_no_state(self, registry):
        """``has_state`` must look at entries, not file existence: a husk
        left behind by an old writer (or one race-written empty) is "no
        state" so the engine's upkeep loop can stand down."""
        registry.path.parent.mkdir(parents=True, exist_ok=True)
        registry.path.write_text(json.dumps({"schemaVersion": 1, "sessions": {}}))
        assert registry.has_state() is False
        registry.path.write_text("{not json")
        assert registry.has_state() is False

    def test_true_for_leftover_session_dir_with_no_registry_row(self, registry):
        (registry.root / "auto-0000beef").mkdir(parents=True)
        assert registry.has_state() is True


class TestLivenessAndBusy:
    def test_reservation_without_claude_record_counts_as_busy(self, registry):
        registry.allocate("auto-00000001", _always(B), pid=os.getpid(), proc_start=None)
        registry.allocate("auto-00000002", _always(B), pid=os.getpid(), proc_start=None)
        seen: dict = {}

        def choose(busy):
            seen.update(busy)
            return (A, "backup")

        registry.allocate("auto-00000003", choose, pid=os.getpid(), proc_start=None)
        assert seen == {B: 2}
        assert registry.busy_counts(registry.live_entries()) == {B: 2, A: 1}

    def test_idle_session_does_not_count_as_busy(self, registry):
        """The busy count that feeds balance's projected 5h-window load is
        about busy sessions: an idle one holds a profile but is not
        spending its account's 5h window, so ``busy_counts`` — the
        definition placement and the lane-0 ranking both use — must not
        count it even though it is live."""
        registry.allocate("auto-00000001", _always(B), pid=os.getpid(), proc_start=None)
        session_dir = registry.session_dir("auto-00000001")
        (session_dir / "sessions").mkdir(parents=True)
        (session_dir / "sessions" / f"{os.getpid()}.json").write_text(
            json.dumps({"pid": os.getpid(), "status": "idle"})
        )
        assert registry.busy_counts(registry.live_entries()) == {}

    def test_dead_pid_is_not_busy(self, registry, monkeypatch):
        registry.allocate("auto-00000001", _always(B), pid=DEAD_PID, proc_start=None)
        monkeypatch.setattr(ms, "is_pid_alive", lambda pid: pid != DEAD_PID)
        assert registry.live_entries() == []

    def test_recycled_pid_is_not_busy(self, registry, monkeypatch):
        registry.allocate(
            "auto-00000001", _always(B), pid=os.getpid(),
            proc_start="Mon Sep  1 00:00:00 2025",
        )
        monkeypatch.setattr(ms, "pid_matches_record", lambda pid, stamp: False)
        assert not entry_is_live(registry.get("auto-00000001"))
        assert registry.live_entries() == []

    def test_concurrent_allocations_see_each_other(self, backup_dir):
        barrier = threading.Barrier(2)
        picked: list[AccountRef] = []

        def choose(busy):
            account = min((A, B), key=lambda acct: busy.get(acct, 0))
            time.sleep(0.05)  # widen the window a lock-free allocator would lose
            return (account, "backup")

        def launch(session_id: str) -> None:
            registry = ManagedSessionRegistry(backup_dir)
            barrier.wait()
            entry = registry.allocate(session_id, choose, pid=os.getpid(), proc_start=None)
            picked.append(entry.account)

        threads = [
            threading.Thread(target=launch, args=(sid,))
            for sid in ("auto-00000001", "auto-00000002")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert sorted(a.email for a in picked) == ["a@example.com", "b@example.com"]


def _make_profile(registry: ManagedSessionRegistry, session_id: str) -> Path:
    d = registry.session_dir(session_id)
    ms.create_managed_profile(d, theme="dark")
    return d


def _age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


class TestSweep:
    @pytest.fixture(autouse=True)
    def _macos(self, monkeypatch):
        monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))

    def test_dead_session_removes_dir_keychain_item_and_entry(
        self, registry, block_real_keychain, monkeypatch
    ):
        registry.allocate("auto-00000001", _always(B), pid=DEAD_PID, proc_start=None)
        d = _make_profile(registry, "auto-00000001")
        key = (keychain_service_name(d), macos_keychain.keychain_account_name())
        block_real_keychain.data[key] = "{}"
        monkeypatch.setattr(ms, "is_pid_alive", lambda pid: pid != DEAD_PID)

        swept = registry.sweep().removed

        assert [e.session_id for e in swept] == ["auto-00000001"]
        assert not d.exists()
        assert key not in block_real_keychain.data
        assert registry.get("auto-00000001") is None
        # The removed entry was the registry's only one: sweep must go
        # through `_write`, which unlinks managed.json rather than leaving
        # a `{"sessions": {}}` husk (see TestRegistry.
        # test_remove_to_empty_unlinks_registry_file for the same rule on
        # a plain `remove`).
        assert not registry.path.exists()

    def test_live_session_is_kept(self, registry):
        registry.allocate("auto-00000001", _always(B), pid=os.getpid(), proc_start=None)
        d = _make_profile(registry, "auto-00000001")
        assert registry.sweep().removed == []
        assert d.is_dir()

    def test_sweep_never_follows_shared_history_links(self, registry, tmp_path, monkeypatch):
        history = tmp_path / "shared-projects"
        history.mkdir()
        (history / "keep.jsonl").write_text("x")
        history_file = tmp_path / "shared-history.jsonl"
        history_file.write_text("y")
        registry.allocate("auto-00000001", _always(B), pid=DEAD_PID, proc_start=None)
        d = _make_profile(registry, "auto-00000001")
        (d / "projects").symlink_to(history)
        (d / "history.jsonl").symlink_to(history_file)
        monkeypatch.setattr(ms, "is_pid_alive", lambda pid: pid != DEAD_PID)
        registry.sweep()
        assert not d.exists()
        assert (history / "keep.jsonl").read_text() == "x"
        assert history_file.read_text() == "y"

    def test_stale_orphan_is_removed_fresh_orphan_kept(self, registry):
        stale = _make_profile(registry, "auto-0000dead")
        _age(stale, ms.ORPHAN_GRACE_S + 60)
        fresh = _make_profile(registry, "auto-0000f00d")
        registry.sweep()
        assert not stale.exists()
        assert fresh.is_dir()

    def test_orphan_with_live_claude_record_is_kept(self, registry):
        d = _make_profile(registry, "auto-0000beef")
        (d / "sessions").mkdir()
        (d / "sessions" / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid()}))
        _age(d, ms.ORPHAN_GRACE_S + 60)
        registry.sweep()
        assert d.is_dir()

    def test_per_account_profiles_are_never_touched(self, registry):
        per_account = registry.root / "2-b_example.com"
        per_account.mkdir(parents=True)
        _age(per_account, ms.ORPHAN_GRACE_S + 60)
        registry.sweep()
        assert per_account.is_dir()

    def test_orphan_grace_uses_injected_clock_not_wall_clock(self, backup_dir):
        """``_sweep_orphans`` must age against ``self._clock()``, not
        ``time.time()``. The profile's mtime is left at the real "now" —
        only the injected clock is pushed past the grace period — so this
        would fail (the dir would be kept) if the sweep read the wall
        clock instead."""
        fake_now = time.time() + ms.ORPHAN_GRACE_S + 60
        registry = ManagedSessionRegistry(backup_dir, clock=lambda: fake_now)
        d = _make_profile(registry, "auto-0000dead")
        registry.sweep()
        assert not d.exists()

    def test_sweep_exposes_live_entries_without_a_second_liveness_pass(
        self, registry, monkeypatch
    ):
        """``sweep()`` must return the live set from the same read that
        found the dead entries, not recompute it: each ``entry_is_live``
        check runs ``ps`` on macOS, and a later caller (push-refresh)
        reuses ``.live`` instead of paying for that twice."""
        registry.allocate("auto-00000001", _always(B), pid=DEAD_PID, proc_start=None)
        registry.allocate("auto-00000002", _always(A), pid=os.getpid(), proc_start=None)
        monkeypatch.setattr(ms, "is_pid_alive", lambda pid: pid != DEAD_PID)

        calls: list[str] = []
        real_entry_is_live = ms.entry_is_live

        def counting(entry):
            calls.append(entry.session_id)
            return real_entry_is_live(entry)

        monkeypatch.setattr(ms, "entry_is_live", counting)

        result = registry.sweep()

        assert [e.session_id for e in result.removed] == ["auto-00000001"]
        assert [e.session_id for e in result.live] == ["auto-00000002"]
        assert calls.count("auto-00000001") == 1
        assert calls.count("auto-00000002") == 1

    def test_dead_entry_with_live_child_process_keeps_its_dir(
        self, registry, block_real_keychain, monkeypatch
    ):
        """A dead registry PID does not mean nothing is using the profile
        any more: a child process the dead PID spawned (the Bash tool, a
        detached tmux pane, a nohup'd process) can inherit
        CLAUDE_CONFIG_DIR and keep writing to it after the parent that
        owned the registry entry exits. ``sweep`` must check
        ``profile_is_quiescent`` before deleting the profile, or that
        child's data is gone for good the instant its parent's PID dies.
        The registry entry is still dropped -- it IS dead -- only the
        directory (and its keychain item) survive."""
        registry.allocate("auto-00000001", _always(B), pid=DEAD_PID, proc_start=None)
        d = _make_profile(registry, "auto-00000001")
        (d / "sessions").mkdir()
        (d / "sessions" / f"{os.getpid()}.json").write_text(
            json.dumps({"pid": os.getpid()})
        )
        key = (keychain_service_name(d), macos_keychain.keychain_account_name())
        block_real_keychain.data[key] = "{}"
        monkeypatch.setattr(ms, "is_pid_alive", lambda pid: pid != DEAD_PID)

        swept = registry.sweep().removed

        assert [e.session_id for e in swept] == ["auto-00000001"]
        assert registry.get("auto-00000001") is None
        assert d.is_dir()
        assert key in block_real_keychain.data

    def test_an_unreadable_registry_sweeps_nothing(
        self, registry, block_real_keychain, monkeypatch
    ):
        """The destructive pass must not run on a registry it could not
        read. `_read` folds "absent" and "unreadable" into an empty dict --
        right for the pollers, fatal here: with no rows to drop, the empty
        `known` set makes every LIVE session's profile look like an
        untracked orphan, and the orphan pass deletes the directory and its
        keychain item out from under a running Claude. The remaining gates
        do not save it: the grace window is about the directory's age, and
        `profile_is_quiescent` sees nothing in a session that has not
        written a readable record yet (the reservation-to-startup gap a
        trust prompt holds open, a long `claude -p` run)."""
        registry.allocate("auto-00000001", _always(B), pid=os.getpid(), proc_start=None)
        d = _make_profile(registry, "auto-00000001")
        (d / "keep.txt").write_text("live session data")
        _age(d, ms.ORPHAN_GRACE_S + 60)
        key = (keychain_service_name(d), macos_keychain.keychain_account_name())
        block_real_keychain.data[key] = "{}"
        truncated = registry.path.read_text()[:20]
        registry.path.write_text(truncated)

        result = registry.sweep()

        assert (result.removed, result.live) == ([], [])
        assert (d / "keep.txt").read_text() == "live session data"
        assert key in block_real_keychain.data
        assert registry.path.read_text() == truncated

    def test_the_orphan_pass_refuses_an_unreadable_registry_on_its_own(
        self, registry, block_real_keychain
    ):
        """The one place "not in `known`" becomes an rmtree checks for
        itself that `known` came from a read that succeeded, rather than
        trusting its caller: an empty set from a READABLE empty registry is
        every auto-* directory really being untracked, an empty set from a
        failed read is not."""
        d = _make_profile(registry, "auto-0000dead")
        _age(d, ms.ORPHAN_GRACE_S + 60)
        registry.path.write_text("{not json")

        assert registry._sweep_orphans(set()) == []
        assert d.is_dir()

    def test_a_readable_empty_registry_still_reclaims_orphans(self, registry):
        """The guard is about "could not read", not about "no rows": a
        registry that reads cleanly with nothing in it must still let the
        orphan pass reclaim a stale untracked profile."""
        registry.path.parent.mkdir(parents=True, exist_ok=True)
        registry.path.write_text(json.dumps({"schemaVersion": 1, "sessions": {}}))
        d = _make_profile(registry, "auto-0000dead")
        _age(d, ms.ORPHAN_GRACE_S + 60)

        registry.sweep()

        assert not d.exists()

    def test_sweep_orphans_skips_a_symlink_even_when_aged(
        self, registry, tmp_path, block_real_keychain
    ):
        """``_sweep_orphans``'s ``path.is_symlink()`` check must exclude a
        symlinked ``auto-*`` name from the orphan reclaim however old it
        looks -- both the link itself (aged via ``follow_symlinks=False``)
        AND its target are aged, so the age check alone (``path.stat()``
        follows the link to the target's mtime) cannot accidentally
        filter this case out and hide a missing ``is_symlink()`` guard --
        and it must never reach ``remove_managed_profile`` for that path,
        proven by seeding a keychain entry under the link's own hashed
        service name and checking it survives."""
        target = tmp_path / "elsewhere"
        target.mkdir()
        (target / "keep.txt").write_text("z")
        _age(target, ms.ORPHAN_GRACE_S + 60)
        registry.root.mkdir(parents=True, exist_ok=True)
        link = registry.root / "auto-0000abcd"
        link.symlink_to(target)
        old = time.time() - (ms.ORPHAN_GRACE_S + 60)
        os.utime(link, (old, old), follow_symlinks=False)
        key = (keychain_service_name(link), macos_keychain.keychain_account_name())
        block_real_keychain.data[key] = "{}"

        registry.sweep()

        assert link.is_symlink()
        assert target.is_dir()
        assert (target / "keep.txt").read_text() == "z"
        assert key in block_real_keychain.data


class TestProfileAndHooks:
    def test_create_profile_is_private_and_onboarded(self, registry):
        d = _make_profile(registry, "auto-00000001")
        assert d.stat().st_mode & 0o777 == 0o700
        assert json.loads((d / ".claude.json").read_text()) == {
            "hasCompletedOnboarding": True, "theme": "dark",
        }

    def test_create_profile_raises_on_existing_dir(self, registry):
        """A managed id is freshly minted per launch (``new_session_id``),
        so an existing dir at that path means something else already put
        data there. Reusing it silently -- keeping whatever mode or
        contents it already had -- would break the "fresh 0700 profile"
        this promises, so it must raise instead."""
        d = registry.session_dir("auto-00000001")
        d.mkdir(parents=True)
        (d / "stray.txt").write_text("preexisting")
        with pytest.raises(FileExistsError):
            ms.create_managed_profile(d, theme="dark")

    def test_create_profile_collision_leaves_existing_keychain_item_intact(
        self, registry, block_real_keychain
    ):
        """A colliding ``session_dir`` belongs to whatever already put data
        there, keychain item included. ``create_managed_profile`` must not
        delete that item until the ``mkdir`` proves the directory is ours --
        deleting it before the collision is detected would corrupt a
        profile this function was specifically designed not to touch."""
        d = registry.session_dir("auto-00000001")
        d.mkdir(parents=True)
        (d / "stray.txt").write_text("preexisting")
        key = (keychain_service_name(d), macos_keychain.keychain_account_name())
        block_real_keychain.data[key] = "{}"

        with pytest.raises(FileExistsError):
            ms.create_managed_profile(d, theme="dark")

        assert key in block_real_keychain.data

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_remove_profile_logs_and_reports_a_denied_child_instead_of_hiding_it(
        self, registry, caplog
    ):
        """``remove_managed_profile`` must not swallow a real removal
        failure the way ``rmtree(ignore_errors=True)`` used to: a denied
        child logs a warning and the directory survives, and the return
        value tells the caller it is not actually gone (instead of the
        old code's ``None``, which a caller checking ``is False`` would
        have misread as "gone")."""
        d = _make_profile(registry, "auto-00000001")
        locked = d / "locked"
        locked.mkdir()
        leaf = locked / "leaf.txt"
        leaf.write_text("keep")
        locked.chmod(0o500)
        try:
            with caplog.at_level("WARNING", logger="claude-swap"):
                gone = ms.remove_managed_profile(d)
            assert gone is False
            assert leaf.exists()
            assert leaf.read_text() == "keep"
            assert any(
                str(leaf) in record.getMessage() for record in caplog.records
            )
        finally:
            locked.chmod(0o700)

    def test_hooks_file_is_an_empty_settings_document(self, registry):
        """Empty, and harmless to pass: `--settings` outranks the user's own
        settings file, but Claude Code merges hook entries across settings
        levels instead of replacing them, so an empty map registers nothing
        and suppresses none of the user's own hooks."""
        d = _make_profile(registry, "auto-00000001")
        path = ms.write_hooks_file(d)
        assert path == d / "cswap-hooks.json"
        assert json.loads(path.read_text()) == {"hooks": {}}

    def test_remove_profile_refuses_a_path_it_was_not_told_it_owns(
        self, registry, caplog
    ):
        """Every caller derives the path from a session id it owns, so a name
        that is not a managed session id is a bug about to rmtree somebody
        else's directory -- the per-account `<num>-<slug>` profiles and the
        sessions root itself sit right beside the managed ones."""
        per_account = registry.root / "2-b_example.com"
        per_account.mkdir(parents=True)
        (per_account / "keep.txt").write_text("not ours")

        with caplog.at_level("WARNING", logger="claude-swap"):
            assert ms.remove_managed_profile(per_account) is False

        assert (per_account / "keep.txt").read_text() == "not ours"
        assert any("Refusing to remove" in r.getMessage() for r in caplog.records)


class TestDescribeSessions:
    SEQUENCE = {"accounts": {"2": {"email": "b@example.com", "organizationUuid": "org-b"}}}

    def test_registered_starting_and_unknown_account(self, registry):
        registry.allocate("auto-00000001", _always(B), pid=os.getpid(), proc_start=None)
        d = _make_profile(registry, "auto-00000001")
        (d / "sessions").mkdir()
        (d / "sessions" / f"{os.getpid()}.json").write_text(json.dumps({
            "pid": os.getpid(), "cwd": "/work/app", "status": "idle",
            "statusUpdatedAt": 1_758_000_000_000,
        }))
        registry.allocate("auto-00000002", _always(A), pid=os.getpid(), proc_start=None)
        _make_profile(registry, "auto-00000002")

        views = {v.entry.session_id: v for v in ms.describe_sessions(registry, self.SEQUENCE)}

        first = views["auto-00000001"]
        assert (first.number, first.status, first.cwd, first.idle_since_ms) == (
            "2", "idle", "/work/app", 1_758_000_000_000,
        )
        second = views["auto-00000002"]
        assert (second.number, second.status, second.idle_since_ms) == (None, "starting", None)

    def test_dead_entries_are_not_listed_and_not_removed(self, registry, monkeypatch):
        registry.allocate("auto-00000001", _always(B), pid=DEAD_PID, proc_start=None)
        monkeypatch.setattr(ms, "is_pid_alive", lambda pid: pid != DEAD_PID)
        assert ms.describe_sessions(registry, self.SEQUENCE) == []
        assert "auto-00000001" in registry.entries()

    def test_orders_by_created_at_not_registry_storage_order(self, backup_dir):
        """The registry stores (and reads back) entries sorted by session id
        (``_write`` sorts its ``sessions`` map), which is not launch order.
        Picking ids whose alphabetical order is the reverse of their
        ``created_at`` order tells the two apart: only a sort keyed on
        ``created_at`` gets this right."""
        times = iter([2_000_000_000.0, 1_000_000_000.0])
        registry = ManagedSessionRegistry(backup_dir, clock=lambda: next(times))
        registry.allocate("auto-0000aaaa", _always(B), pid=os.getpid(), proc_start=None)
        registry.allocate("auto-0000bbbb", _always(A), pid=os.getpid(), proc_start=None)

        views = ms.describe_sessions(registry, self.SEQUENCE)

        assert [v.entry.session_id for v in views] == ["auto-0000bbbb", "auto-0000aaaa"]

    def test_non_string_status_and_cwd_read_as_unknown(self, registry):
        registry.allocate("auto-00000001", _always(B), pid=os.getpid(), proc_start=None)
        d = _make_profile(registry, "auto-00000001")
        (d / "sessions").mkdir()
        (d / "sessions" / f"{os.getpid()}.json").write_text(json.dumps({
            "pid": os.getpid(), "status": 7, "cwd": 9,
        }))

        [view] = ms.describe_sessions(registry, self.SEQUENCE)

        assert (view.status, view.cwd) == ("unknown", "")

    def test_out_of_range_status_updated_at_drops_idle_since(self, registry):
        """A unit bug in Claude's record (statusUpdatedAt in nanoseconds,
        not milliseconds) must not reach datetime.fromtimestamp downstream
        (json_output._timestamp) -- describe_sessions bounds it away here,
        at the point the value is produced, instead."""
        registry.allocate("auto-00000001", _always(B), pid=os.getpid(), proc_start=None)
        d = _make_profile(registry, "auto-00000001")
        (d / "sessions").mkdir()
        (d / "sessions" / f"{os.getpid()}.json").write_text(json.dumps({
            "pid": os.getpid(), "status": "idle",
            "statusUpdatedAt": 1_758_000_000_000_000_000,  # ns-scale, not ms
        }))

        [view] = ms.describe_sessions(registry, self.SEQUENCE)

        assert view.status == "idle"
        assert view.idle_since_ms is None


class TestReadSessionState:
    def _write_record(self, session_dir, pid, payload):
        (session_dir / "sessions").mkdir(parents=True, exist_ok=True)
        (session_dir / "sessions" / f"{pid}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_no_record_yet(self, tmp_path):
        state = ms.read_session_state(tmp_path, 4242)
        assert (state.has_record, state.status, state.idle_since_ms, state.cwd) == (
            False, None, None, "",
        )

    def test_idle_record(self, tmp_path):
        self._write_record(tmp_path, 4242, {
            "pid": 4242, "status": "idle", "cwd": "/work",
            "statusUpdatedAt": 1_758_000_000_000,
        })
        state = ms.read_session_state(tmp_path, 4242)
        assert (state.has_record, state.status, state.idle_since_ms, state.cwd) == (
            True, "idle", 1_758_000_000_000, "/work",
        )

    def test_busy_record_has_no_idle_since(self, tmp_path):
        self._write_record(tmp_path, 4242, {
            "pid": 4242, "status": "busy", "statusUpdatedAt": 1_758_000_000_000,
        })
        state = ms.read_session_state(tmp_path, 4242)
        assert (state.status, state.idle_since_ms) == ("busy", None)

    def test_unusable_status_keeps_the_record_flag(self, tmp_path):
        """A record with a non-string status still proves Claude registered:
        entry_is_busy must keep counting it, exactly as before."""
        self._write_record(tmp_path, 4242, {"pid": 4242, "status": 7})
        state = ms.read_session_state(tmp_path, 4242)
        assert (state.has_record, state.status) == (True, None)


class TestManagedSessionIdFor:
    def test_a_managed_profile_resolves(self, tmp_path):
        root = ms.sessions_root(tmp_path)
        (root / "auto-0123abcd").mkdir(parents=True)
        assert ms.managed_session_id_for(
            str(root / "auto-0123abcd"), tmp_path
        ) == "auto-0123abcd"

    def test_a_trailing_slash_still_resolves(self, tmp_path):
        root = ms.sessions_root(tmp_path)
        (root / "auto-0123abcd").mkdir(parents=True)
        assert ms.managed_session_id_for(
            f"{root / 'auto-0123abcd'}/", tmp_path
        ) == "auto-0123abcd"

    def test_a_run_profile_does_not(self, tmp_path):
        root = ms.sessions_root(tmp_path)
        (root / "2-work").mkdir(parents=True)
        assert ms.managed_session_id_for(str(root / "2-work"), tmp_path) is None

    def test_a_managed_name_outside_the_sessions_root_does_not(self, tmp_path):
        elsewhere = tmp_path / "elsewhere" / "auto-0123abcd"
        elsewhere.mkdir(parents=True)
        assert ms.managed_session_id_for(str(elsewhere), tmp_path) is None

    def test_the_default_login_does_not(self, tmp_path):
        assert ms.managed_session_id_for(str(tmp_path / ".claude"), tmp_path) is None

    def test_a_symlinked_backup_root_still_resolves(self, tmp_path):
        """The launched-with string is what identifies a session, so a config
        dir reached through a symlinked backup root is the same session —
        which only ``samefile`` can tell, since the two paths differ."""
        real = tmp_path / "real"
        (ms.sessions_root(real) / "auto-0123abcd").mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        assert ms.managed_session_id_for(
            str(ms.sessions_root(link) / "auto-0123abcd"), real
        ) == "auto-0123abcd"

    def test_unset_config_dir_does_not(self, tmp_path):
        assert ms.managed_session_id_for(None, tmp_path) is None
        assert ms.managed_session_id_for("", tmp_path) is None
