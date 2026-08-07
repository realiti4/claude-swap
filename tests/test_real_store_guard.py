"""A test process must be structurally unable to write the REAL account
store — not merely warned not to, because a guard that unwinds (any
``patch``/``monkeypatch`` fixture) is gone by the time a background thread
that outlived its own test's teardown gets around to writing.

Measured incident: ``sequence.json``/``credentials/*.enc`` under the real
``~/.local/share/claude-swap`` were overwritten at 03:26 with the exact
``a@example.com``/``b@example.com`` pair ``EngineHarness.seed`` (this repo,
``tests/test_autoswitch.py``) writes. ``tests/conftest.py``'s ``temp_home``
and ``_isolate_real_home`` fixtures use ``patch.dict``/``monkeypatch`` as
context managers, which unwind at teardown; a thread started inside a test
that survives past that teardown sees the REAL ``$HOME`` (``pathlib.Path.home``
is process-global, not thread-local), because the patch is gone by the time
it runs.

The fix under test: ``conftest.py`` installs a process-global
``sys.addaudithook`` (module import time, no removal API — it cannot be
unwound the way a fixture patch can) that refuses any WRITE-mode ``open``/
``os.rename``/``os.mkdir``/``os.remove``/``os.rmdir`` whose target resolves,
AT THE MOMENT OF THE CALL, under the REAL (currently-computed, not cached)
``claude_swap.paths`` roots — regardless of which thread performs it.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from claude_swap import paths
from claude_swap.models import Platform
from tests import conftest


def test_control_a_tmp_path_write_is_allowed(tmp_path: Path):
    """CONTROL A: the guard must not block everything — a legitimate write
    to pytest's own isolated tmp_path must still succeed."""
    target = tmp_path / "control-a-allowed.txt"
    target.write_text("ok", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "ok"


def test_control_b_and_c_real_store_write_is_refused(monkeypatch):
    """CONTROL B (main thread) and CONTROL C (a thread that outlives its
    own test's isolation, the case that actually matters) both attempt a
    write under the REAL ``claude_swap.paths.get_backup_root()`` and must
    both be refused — never silently succeed, never silently no-op.

    ``monkeypatch.undo()`` reverses the autouse ``_isolate_real_home``
    fixture's patches (it shares this test's ``monkeypatch`` instance —
    the same mechanism ``test_move_strict_clear_fails_closed_on_locked_
    keychain`` already uses elsewhere in this suite), exposing the TRUE,
    unpatched ``$HOME``/``Path.home()`` for the rest of this test body —
    exactly the state a thread sees after its own test's teardown has run.
    """
    from claude_swap.exceptions import ClaudeSwitchError  # noqa: F401  (sanity import only)

    marker_name = ".cswap-test-real-store-guard-probe-DELETE-ME"

    monkeypatch.undo()  # expose the REAL, unpatched HOME from here on

    real_backup_root = paths.get_backup_root()
    real_marker = real_backup_root / marker_name
    if real_marker.exists():
        real_marker.unlink()  # defensive: a prior failed run left one behind

    # -- CONTROL B: main thread --------------------------------------
    outcome_main: dict = {}
    try:
        real_marker.write_text("probe\n", encoding="utf-8")
        outcome_main["wrote"] = True
    except conftest.RealStoreWriteBlocked as e:
        outcome_main["wrote"] = False
        outcome_main["error"] = e

    try:
        assert outcome_main["wrote"] is False, (
            "CONTROL B FAILED: a main-thread write to the REAL backup root "
            f"({real_marker}) was not refused"
        )
        assert not real_marker.exists(), (
            "the write was reported as refused but the file exists anyway"
        )
    finally:
        if real_marker.exists():
            real_marker.unlink()  # never leave real-store litter, pass or fail

    # -- CONTROL C: a thread that outlives its own test's teardown ---
    # (the case the incident actually was: a thread started while isolation
    # was active, but not joined before the isolating patches unwound).
    release = threading.Event()
    outcome_thread: dict = {}

    def leaked_write():
        release.wait(timeout=5)
        target = paths.get_backup_root() / marker_name
        try:
            target.write_text("probe\n", encoding="utf-8")
            outcome_thread["wrote"] = True
        except conftest.RealStoreWriteBlocked as e:
            outcome_thread["wrote"] = False
            outcome_thread["error"] = e

    t = threading.Thread(target=leaked_write, daemon=True)
    t.start()
    release.set()
    t.join(timeout=5)

    try:
        assert not t.is_alive(), "the probe thread did not finish in time"
        assert "wrote" in outcome_thread, "the probe thread never reached its write"
        assert outcome_thread["wrote"] is False, (
            "CONTROL C FAILED (the case that matters): a background thread's "
            f"write to the REAL backup root ({real_marker}) was not refused"
        )
        assert not real_marker.exists(), (
            "the thread's write was reported as refused but the file exists anyway"
        )
    finally:
        if real_marker.exists():
            real_marker.unlink()


def test_rmtree_of_a_protected_root_is_refused_before_any_child_is_removed(
    tmp_path: Path, monkeypatch
):
    """C-1: ``shutil.rmtree`` walks via ``os.scandir``/``dir_fd`` and unlinks
    children by RELATIVE name (``os.remove('seq.json', dir_fd=...)``) — only
    the outermost ``os.rmdir(path)`` carries an absolute path, so hooking the
    per-child ``os.remove``/``os.rmdir`` events (as every other write shape
    in this guard does) lets every child vanish before the guard ever fires;
    the ``RealStoreWriteBlocked`` it eventually raises reports a refusal it
    did not perform. A stand-in root is registered into ``_REAL_STORE_SPECS``
    (never the real store) so this test is safe against the developer's
    actual account data regardless of isolation state.
    """
    stand_in_root = tmp_path / "claude-swap"
    stand_in_root.mkdir()
    (stand_in_root / "configs").mkdir()
    (stand_in_root / "configs" / ".claude-config-1-a@example.com.json").write_text("{}")
    (stand_in_root / "credentials").mkdir()
    (stand_in_root / "credentials" / ".creds-1-a@example.com.enc").write_text("x")
    (stand_in_root / "sequence.json").write_text("{}")

    monkeypatch.setattr(conftest, "_REAL_STORE_SPECS", ((stand_in_root, True),))

    entries_before = sorted(
        str(p.relative_to(stand_in_root)) for p in stand_in_root.rglob("*")
    )
    assert len(entries_before) == 5, entries_before

    with pytest.raises(conftest.RealStoreWriteBlocked):
        shutil.rmtree(stand_in_root)

    entries_after = (
        sorted(str(p.relative_to(stand_in_root)) for p in stand_in_root.rglob("*"))
        if stand_in_root.exists()
        else []
    )
    assert entries_after == entries_before, (
        f"guard raised but data was already gone: before={entries_before} "
        f"after={entries_after}"
    )


# -- m-1: five previously-untested guard shapes (M7/M8/M9/M11/M12) --------
#
# `test_control_b_and_c_real_store_write_is_refused` exercises exactly one
# shape (a pathlib mode-string ``write_text`` into the recursive backup
# root) — it never touches ``os.mkdir``/``os.remove``/``os.rmdir`` directly,
# never an ``os.open`` flags-only call, never a non-recursive root, and
# never the env-neutralization. Each mutation below survived a full run for
# exactly that reason.


def test_os_mkdir_and_os_remove_into_protected_root_are_refused(
    tmp_path: Path, monkeypatch
):
    """M7: narrowing ``_WRITE_EVENTS`` to ``{"open"}`` survived because
    nothing exercised ``os.mkdir``/``os.remove`` directly (only through
    ``pathlib``'s own ``open``-backed ``write_text``/``unlink``)."""
    stand_in_root = tmp_path / "claude-swap"
    stand_in_root.mkdir()
    target = stand_in_root / "sequence.json"
    target.write_text("{}")  # seeded before the guard is armed on this root

    monkeypatch.setattr(conftest, "_REAL_STORE_SPECS", ((stand_in_root, True),))

    new_dir = stand_in_root / "new_subdir"
    with pytest.raises(conftest.RealStoreWriteBlocked):
        os.mkdir(new_dir)
    assert not new_dir.exists()

    with pytest.raises(conftest.RealStoreWriteBlocked):
        os.remove(target)
    assert target.exists()


def test_os_open_flags_only_write_into_protected_root_is_refused(
    tmp_path: Path, monkeypatch
):
    """M8: the ``os.open`` flags-only branch of ``_is_write_open`` (no
    ``mode`` string — only ``flags`` says WRITE) returning ``False``
    survived because every write in the suite goes through ``pathlib``,
    which always supplies a ``mode`` string."""
    stand_in_root = tmp_path / "claude-swap"
    stand_in_root.mkdir()
    monkeypatch.setattr(conftest, "_REAL_STORE_SPECS", ((stand_in_root, True),))

    target = stand_in_root / "sequence.json"
    fd = None
    try:
        with pytest.raises(conftest.RealStoreWriteBlocked):
            fd = os.open(target, os.O_WRONLY | os.O_CREAT)
    finally:
        if fd is not None:
            os.close(fd)
    assert not target.exists()


def test_non_recursive_root_protects_only_direct_children(
    tmp_path: Path, monkeypatch
):
    """M9: collapsing the recursive/non-recursive split to always-recursive
    survived because the suite's one guard test only ever writes into a
    RECURSIVE root — a non-recursive root (``~/.claude``) must still permit
    a deeply nested write (a job worktree under ``~/.claude/jobs/...``)
    while refusing a direct child (``.credentials.json``)."""
    non_recursive_root = tmp_path / ".claude"
    deep_dir = non_recursive_root / "jobs" / "abc" / "tmp"
    deep_dir.mkdir(parents=True)  # created before the guard is armed on it

    monkeypatch.setattr(conftest, "_REAL_STORE_SPECS", ((non_recursive_root, False),))

    deep_target = deep_dir / "somefile.json"
    deep_target.write_text("ok", encoding="utf-8")  # must NOT raise
    assert deep_target.exists()

    direct_target = non_recursive_root / ".credentials.json"
    with pytest.raises(conftest.RealStoreWriteBlocked):
        direct_target.write_text("ok", encoding="utf-8")
    assert not direct_target.exists()


def test_frozen_specs_include_the_two_non_recursive_roots(monkeypatch, tmp_path):
    """M11: dropping the four non-recursive-root entries survived because
    no test inspects ``_REAL_STORE_SPECS`` directly — these two roots
    (``~/.claude``, ``$HOME``) are the ONLY protection for
    ``~/.claude/.credentials.json`` and ``~/.claude.json``."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    for var in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "XDG_DATA_HOME"):
        monkeypatch.delenv(var, raising=False)

    specs = conftest._freeze_real_store_specs()
    non_recursive_roots = {root for root, recursive in specs if not recursive}

    assert home / ".claude" in non_recursive_roots
    assert home in non_recursive_roots


def test_frozen_specs_ignore_a_developer_exported_claude_config_dir(
    monkeypatch, tmp_path
):
    """M12: removing the env-neutralization inside ``_freeze_real_store_specs``
    survived because no test exports ``CLAUDE_CONFIG_DIR`` around the call —
    a developer with it set in their normal shell would otherwise get ONLY the
    override path protected, silently dropping real ``~/.claude`` protection
    (the env-neutralization is what produces the separate DEFAULT snapshot at
    all). Both roots must be protected now: the override IS also a real
    account-store location for a developer who has it exported (the same
    both-must-be-protected reasoning the XDG_DATA_HOME fix applies) — this
    updated assertion reflects that; only the default-snapshot regression
    (dropping ``~/.claude``) is what the mutation below still needs to kill.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    elsewhere = home / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))

    specs = conftest._freeze_real_store_specs()
    non_recursive_roots = {root for root, recursive in specs if not recursive}

    assert home / ".claude" in non_recursive_roots, (
        "the genuinely-default ~/.claude must still be protected even with "
        "CLAUDE_CONFIG_DIR exported"
    )
    assert elsewhere in non_recursive_roots, (
        "the override path must ALSO be protected — a developer with "
        "CLAUDE_CONFIG_DIR exported has their real config home there"
    )


def test_frozen_specs_include_the_ambient_xdg_override_backup_root(
    monkeypatch, tmp_path
):
    """`_freeze_real_store_specs` clears XDG_DATA_HOME before resolving, so on
    a machine where it's exported OUTSIDE $HOME, the real account store lives
    at the override path and this snapshot never included it — the defaults
    snapshot alone is not enough. The frozen set must ALSO contain the root
    `claude_swap.paths` resolves to under the environment as it actually is."""
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg-outside-home"  # deliberately NOT under `home`
    xdg.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr(Platform, "detect", staticmethod(lambda: Platform.LINUX))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    specs = conftest._freeze_real_store_specs()
    recursive_roots = {root for root, recursive in specs if recursive}

    assert xdg / "claude-swap" in recursive_roots, (
        "the XDG-override backup root must be protected, not only the "
        "cleared-env default ~/.local/share/claude-swap"
    )
    assert home / ".local" / "share" / "claude-swap" in recursive_roots, (
        "the genuinely-default root must still be protected too"
    )


def test_layout_a_runtime_real_store_is_refused_and_unrelated_tmp_still_writes(
    monkeypatch, tmp_path
):
    """The mandatory YES/NO probe from the finding: under layout A (XDG_DATA_HOME
    exported outside $HOME), a write to the RUNTIME real store (what
    `paths.get_backup_root()` actually resolves to under this environment)
    must be refused (YES-arm) while a write to an unrelated tmp path must
    still succeed (NO-arm) — a guard that refuses everything is not a fix.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg-outside-home"
    xdg.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr(Platform, "detect", staticmethod(lambda: Platform.LINUX))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    # Install the specs a real conftest import would freeze under THIS
    # (simulated) ambient environment — this is what the fix under test
    # changes; the live audit hook reads `conftest._REAL_STORE_SPECS` by
    # module-global lookup at call time, so this patch governs its behavior.
    monkeypatch.setattr(conftest, "_REAL_STORE_SPECS", conftest._freeze_real_store_specs())

    runtime_real_store = paths.get_backup_root()
    assert runtime_real_store == xdg / "claude-swap"  # sanity: the hole's target

    # YES-arm: the runtime real store must be refused.
    yes_target = runtime_real_store / "sequence.json"
    with pytest.raises(conftest.RealStoreWriteBlocked):
        yes_target.parent.mkdir(parents=True, exist_ok=True)
        yes_target.write_text("{}", encoding="utf-8")
    assert not yes_target.exists()

    # NO-arm: an unrelated tmp path must still succeed (the guard isn't a
    # blanket refuse-everything).
    no_target = tmp_path / "unrelated" / "file.txt"
    no_target.parent.mkdir(parents=True, exist_ok=True)
    no_target.write_text("ok", encoding="utf-8")
    assert no_target.read_text(encoding="utf-8") == "ok"


# -- C2: an arbitrary CLAUDE_CONFIG_DIR must not slip past the hint pre-filter --
#
# M3 (narrowing `_REAL_STORE_HINTS`) was KILLED under a `/tmp` basetemp but
# SURVIVED under a basetemp inside `~/.claude` — pytest's own `tmp_path` then
# already contains the substring ".claude" somewhere in its ancestry, so even
# a WRONG hint tuple accidentally matched and the kill was luck, not a
# property of the fix. This test builds its own root via `tempfile.mkdtemp()`
# rather than the `tmp_path` fixture specifically so its verdict cannot
# depend on where pytest's basetemp happens to live.


def test_module_level_hints_are_wired_to_the_derivation_function():
    """Wiring check: the mutation-battery equivalent of asserting
    ``_REAL_STORE_HINTS = _derive_real_store_hints(_REAL_STORE_SPECS)`` is
    still the live assignment, not a hardcoded tuple that happens to agree
    with it today. The two other C2 tests exercise ``_derive_real_store_hints``
    directly against a simulated environment (correctly — that's the actual
    bypass) but neither one would notice if the module-level global were
    quietly reverted to a fixed guess while the (now-orphaned) function stayed
    correct; only comparing the real, currently-imported global against a
    fresh call catches that."""
    assert conftest._REAL_STORE_HINTS == conftest._derive_real_store_hints(
        conftest._REAL_STORE_SPECS, conftest._HOME_AT_FREEZE_TIME
    )


def test_arbitrary_claude_config_dir_is_not_dropped_by_the_hint_prefilter(
    monkeypatch,
):
    """C2: the audit hook's cheap substring pre-filter used to be a fixed
    guess (``(".claude", "claude-swap")``) — correct for the two DEFAULT
    roots, but a real store reached via
    ``CLAUDE_CONFIG_DIR=$HOME/work-profile`` resolves to
    ``~/work-profile/.credentials.json``, which contains neither substring.
    The pre-filter rejected it before the (already-correct) specs loop ever
    ran, so the write went through even though the root IS in
    ``_REAL_STORE_SPECS``.
    """
    root_dir = Path(tempfile.mkdtemp(prefix="cswap-c2-noclaude-"))
    try:
        home = root_dir / "home"
        home.mkdir()
        (home / ".claude").mkdir()
        work_profile = home / "work-profile"
        work_profile.mkdir()

        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(work_profile))
        monkeypatch.setenv("HOME", str(home))

        specs = conftest._freeze_real_store_specs()
        assert work_profile in {r for r, _rec in specs}, (
            "premise: the CLAUDE_CONFIG_DIR-resolved root must actually be "
            "in the frozen specs, or this isn't testing the pre-filter"
        )
        monkeypatch.setattr(conftest, "_REAL_STORE_SPECS", specs)
        monkeypatch.setattr(
            conftest, "_REAL_STORE_HINTS",
            conftest._derive_real_store_hints(specs, home),
        )

        target = work_profile / ".credentials.json"
        assert not any(
            hint in str(target) for hint in (".claude", "claude-swap")
        ), "premise: the target must miss BOTH of the old hardcoded hints"

        with pytest.raises(conftest.RealStoreWriteBlocked):
            target.write_text('{"pwned": true}', encoding="utf-8")
        assert not target.exists()
    finally:
        # `ignore_errors=True` swallows OSError ONLY. I-3 deliberately rebased
        # `RealStoreWriteBlocked` off `PermissionError` so no `except OSError`
        # can swallow a refusal -- which means this cleanup, whose own paths
        # are inside the root this test froze into `_REAL_STORE_SPECS`, now
        # raises out of `finally` instead of being ignored. Measured: the
        # Windows runner failed here with `os.rmdir refused: ...\home\.claude`
        # while Linux/macOS passed, because their tmp roots do not collide.
        #
        # Unfreezing the specs first is the fix rather than catching the
        # exception: the guard must stay armed for the assertion above, and a
        # bare `except Exception` in cleanup is how a real refusal gets hidden.
        monkeypatch.undo()
        shutil.rmtree(root_dir, ignore_errors=True)


# -- I-list: five previously-untested guard bypasses, each measured by hand --


def test_i1_os_rename_source_out_of_protected_root_is_refused(
    tmp_path: Path, monkeypatch
):
    """I-1: the guard checked only the DESTINATION of ``os.rename`` — the
    same shape ``migrate_legacy_backup_dir``/``shutil.move`` uses to
    relocate the legacy backup directory. Renaming the protected root itself
    OUT to an unprotected location must be refused too, or the store simply
    vanishes from where every reader expects it (the same "make it
    disappear" shape the ``shutil.rmtree`` branch above exists for)."""
    stand_in_root = tmp_path / "claude-swap"
    stand_in_root.mkdir()
    (stand_in_root / "sequence.json").write_text("{}")  # seeded before arming

    monkeypatch.setattr(conftest, "_REAL_STORE_SPECS", ((stand_in_root, True),))

    outside = tmp_path / "outside_dst"
    with pytest.raises(conftest.RealStoreWriteBlocked):
        os.rename(stand_in_root, outside)
    assert stand_in_root.exists(), "the protected root must still be at its original path"
    assert not outside.exists()


def test_i2_os_symlink_into_protected_root_is_refused(
    tmp_path: Path, monkeypatch
):
    """I-2: ``os.symlink`` was not even in ``_WRITE_EVENTS``, so a symlink
    planted inside a protected root — aliasing an arbitrary target onto a
    path a reader (Claude Code, cswap itself) would trust as real store
    content — went through untouched."""
    stand_in_root = tmp_path / "claude-swap"
    stand_in_root.mkdir()

    monkeypatch.setattr(conftest, "_REAL_STORE_SPECS", ((stand_in_root, True),))

    attacker_target = tmp_path / "attacker.txt"
    attacker_target.write_text("x")
    link_path = stand_in_root / "evil-link"
    with pytest.raises(conftest.RealStoreWriteBlocked):
        os.symlink(attacker_target, link_path)
    assert not link_path.exists() and not link_path.is_symlink()


def test_i3_relative_path_with_cwd_inside_protected_root_is_refused(
    tmp_path: Path, monkeypatch
):
    """I-3: a relative path is resolved against ``os.getcwd()`` by the
    underlying syscall exactly as much as an absolute path would be — the
    guard's ``if not target.is_absolute(): continue`` let ``open("x", "w")``
    through untouched whenever the process cwd happened to be inside a
    protected root."""
    stand_in_root = tmp_path / "claude-swap"
    stand_in_root.mkdir()

    monkeypatch.setattr(conftest, "_REAL_STORE_SPECS", ((stand_in_root, True),))

    old_cwd = os.getcwd()
    os.chdir(stand_in_root)
    try:
        with pytest.raises(conftest.RealStoreWriteBlocked):
            with open("relative_seq.json", "w") as f:
                f.write("{}")
    finally:
        os.chdir(old_cwd)
    assert not (stand_in_root / "relative_seq.json").exists()


def test_i4_os_truncate_on_protected_root_file_is_refused(
    tmp_path: Path, monkeypatch
):
    """I-4: ``os.truncate`` destroys content in place without going through
    ``open``, so it reached none of the write-mode checks — a bare
    ``os.truncate(path, 0)`` on a protected-root file went through
    untouched."""
    stand_in_root = tmp_path / "claude-swap"
    stand_in_root.mkdir()
    target = stand_in_root / "sequence.json"
    target.write_text('{"accounts": {"1": "a"}}')  # seeded before arming

    monkeypatch.setattr(conftest, "_REAL_STORE_SPECS", ((stand_in_root, True),))

    with pytest.raises(conftest.RealStoreWriteBlocked):
        os.truncate(target, 0)
    assert target.stat().st_size > 0, "the file must not have been truncated"


def test_i5_bytes_path_into_protected_root_is_refused(
    tmp_path: Path, monkeypatch
):
    """I-5: a ``bytes`` path (``open(b"/path", "wb")``) reached the
    substring pre-filter as ``hint in candidate`` where ``candidate`` was
    ``bytes`` and every hint is ``str`` — never equal, so the check silently
    always missed rather than raising, and the write went through."""
    stand_in_root = tmp_path / "claude-swap"
    stand_in_root.mkdir()
    target = stand_in_root / "sequence.json"
    target.write_text('{"accounts": {"1": "a"}}')  # seeded before arming

    monkeypatch.setattr(conftest, "_REAL_STORE_SPECS", ((stand_in_root, True),))

    bpath = os.fsencode(str(target))
    with pytest.raises(conftest.RealStoreWriteBlocked):
        with open(bpath, "wb") as f:
            f.write(b"OVERWRITTEN")
    assert target.read_text() == '{"accounts": {"1": "a"}}', (
        "the file must not have been overwritten via a bytes path"
    )


def test_derived_hints_exclude_the_bare_home_root_basename():
    """C2 follow-up: deriving hints from every frozen root's basename would,
    without an exclusion, include ``Path.home()``'s own basename — the
    developer's OS username — as a pre-filter substring. Almost every path
    on the machine contains the username (``/tmp/pytest-of-<user>/...``, any
    project under the home directory), so that would make the "cheap
    reject" reject almost nothing, defeating the pre-filter's purpose. The
    bare-home root's only protected children are the hardcoded
    ``.claude*``-prefixed files already caught by the ``.claude`` floor
    hint, so excluding it costs nothing.
    """
    home = Path("/home/some-real-looking-username")
    specs = (
        (home / ".local" / "share" / "claude-swap", True),
        (home / ".claude", False),
        (home, False),  # the bare-home root itself
    )
    hints = conftest._derive_real_store_hints(specs, home)
    assert home.name not in hints, (
        f"DEFECT: {home.name!r} (the username) is in the pre-filter hints "
        "— this would make the cheap reject match almost every path on "
        "the machine"
    )
    # Sanity: the bare-home root's real protected children still match via
    # the unconditional '.claude' floor hint.
    assert any(hint in str(home / ".claude.json") for hint in hints)


def test_mkdir_exist_ok_true_does_not_swallow_the_refusal(
    tmp_path: Path, monkeypatch
):
    """I-3: ``RealStoreWriteBlocked`` subclasses ``PermissionError`` (an
    ``OSError``), so ``pathlib.Path.mkdir(parents=True, exist_ok=True)``
    catches it via its own ``except OSError:`` (when the directory already
    exists) and returns normally instead of propagating -- the guard fired,
    but the caller ate the refusal and proceeded as if nothing happened.
    That is the exact shape ``cache.write_cache`` / ``_atomic_b64_write`` /
    ``_update_global_config`` all use.

    A absent-dir: refuses and raises (mkdir's own bootstrap case).
    B existing-dir: must ALSO raise -- today it is swallowed instead.
    C control: a plain file write into the same protected dir is still
       refused, proving the guard itself is armed on this root (this is
       "the hook fired and the caller ate it", not "the hook is off").
    """
    stand_in_root = tmp_path / "claude-swap"
    monkeypatch.setattr(conftest, "_REAL_STORE_SPECS", ((stand_in_root, True),))

    # A: absent dir -- mkdir(exist_ok=True) must refuse and raise.
    with pytest.raises(conftest.RealStoreWriteBlocked):
        stand_in_root.mkdir(parents=True, exist_ok=True)
    assert not stand_in_root.exists()

    # Seed the dir OUTSIDE the guard's view (os.mkdir is unguarded here only
    # via direct filesystem bootstrap, matching how the real backup root
    # exists on every developer machine before cswap ever runs in-process).
    monkeypatch.setattr(conftest, "_REAL_STORE_SPECS", ())
    stand_in_root.mkdir(parents=True)
    monkeypatch.setattr(conftest, "_REAL_STORE_SPECS", ((stand_in_root, True),))

    # B: existing dir -- must ALSO refuse and raise, not swallow.
    with pytest.raises(conftest.RealStoreWriteBlocked):
        stand_in_root.mkdir(parents=True, exist_ok=True)

    # C: control -- a plain write into the same root is still refused,
    # proving the guard is armed (isolates "swallowed" from "never fired").
    with pytest.raises(conftest.RealStoreWriteBlocked):
        (stand_in_root / "sequence.json").write_text("{}", encoding="utf-8")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "the third snapshot's mechanism is `Path.home()` falling back to the "
        "POSIX pwd database once $HOME is cleared; Windows resolves the home "
        "from USERPROFILE and has no `pwd` module, so the shape under test "
        "does not exist there"
    ),
)
def test_c0_a_scratch_home_still_protects_the_os_account_home_store(monkeypatch, tmp_path):
    import pwd

    # The autouse `_isolate_real_home` fixture ALSO monkeypatches
    # `pathlib.Path.home` to the isolated dir, which defeats the pwd
    # fallback the third snapshot depends on. Restore the real
    # `Path.home` for this test -- $HOME stays scratch, which is the
    # condition under test.
    monkeypatch.undo()
    scratch = tmp_path / "scratch-home"
    scratch.mkdir()
    monkeypatch.setenv("HOME", str(scratch))
    monkeypatch.setenv("USERPROFILE", str(scratch))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    pwd_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    specs = conftest._freeze_real_store_specs()
    roots = [root for root, _recursive in specs]

    assert pwd_home / ".local" / "share" / "claude-swap" in roots, (
        "with $HOME pointed at a scratch dir -- what the mandated isolation "
        "recipe does BEFORE the interpreter starts -- the account's true "
        "store under the OS account home must still be frozen as protected; "
        "otherwise the guard is armed only for a bare-pytest developer and "
        "disarmed for exactly the population running mutation batteries"
    )
    assert scratch / ".local" / "share" / "claude-swap" in roots, (
        "the scratch HOME's own root must stay protected too"
    )
