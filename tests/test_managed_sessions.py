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
from claude_swap.process_detection import process_start_ticks

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
        """An empty registry must not keep upkeep (has_state/sweep_dead)
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
        assert ManagedSessionRegistry.busy_counts(registry.live_entries()) == {B: 2, A: 1}

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
