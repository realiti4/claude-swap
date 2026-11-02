"""Tests for claude_swap.fsutil filesystem primitives."""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

from claude_swap.fsutil import (
    atomic_write_bytes,
    atomic_write_text,
    publish_target,
    read_text_with_retry,
    replace_with_retry,
)

# Captured at import, before conftest's autouse fixture stubs os.fsync per test.
_REAL_FSYNC = os.fsync


class TestReplaceWithRetry:
    """os.replace is not reliably available on Windows: antivirus and the
    search indexer open freshly-written files opportunistically, so a replace
    onto a just-created target fails with ERROR_ACCESS_DENIED/SHARING_VIOLATION
    for a few milliseconds. Measured at ~44% of replaces on a Defender-scanned
    temp dir, which silently broke credential and usage-store writes.
    """

    def _win_oserror(self, winerror: int) -> OSError:
        e = OSError(13, "Access is denied")
        e.winerror = winerror
        return e

    @pytest.mark.parametrize("winerror", [5, 32, 33])
    def test_retries_transient_windows_errors(self, tmp_path, monkeypatch, winerror):
        monkeypatch.setattr("claude_swap.fsutil.sys.platform", "win32")
        calls = []
        real_replace = os.replace

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 4:
                raise self._win_oserror(winerror)
            return real_replace(src, dst)

        monkeypatch.setattr("claude_swap.fsutil.os.replace", flaky)
        src = tmp_path / "tmp.tmp"
        src.write_text("payload")
        dst = tmp_path / "target.json"

        replace_with_retry(src, dst)

        assert len(calls) == 4
        assert dst.read_text() == "payload"

    def test_gives_up_and_raises_after_attempts(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.fsutil.sys.platform", "win32")

        def always_fail(src, dst):
            raise self._win_oserror(5)

        monkeypatch.setattr("claude_swap.fsutil.os.replace", always_fail)
        with pytest.raises(OSError):
            replace_with_retry(tmp_path / "a", tmp_path / "b", attempts=3)

    def test_does_not_retry_real_errors(self, tmp_path, monkeypatch):
        """A genuine failure (e.g. missing source) must surface immediately."""
        monkeypatch.setattr("claude_swap.fsutil.sys.platform", "win32")
        calls = []

        def missing(src, dst):
            calls.append(1)
            raise self._win_oserror(2)  # ERROR_FILE_NOT_FOUND

        monkeypatch.setattr("claude_swap.fsutil.os.replace", missing)
        with pytest.raises(OSError):
            replace_with_retry(tmp_path / "a", tmp_path / "b")
        assert len(calls) == 1

    def test_posix_never_retries(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.fsutil.sys.platform", "linux")
        calls = []

        def fail(src, dst):
            calls.append(1)
            raise self._win_oserror(5)

        monkeypatch.setattr("claude_swap.fsutil.os.replace", fail)
        with pytest.raises(OSError):
            replace_with_retry(tmp_path / "a", tmp_path / "b")
        assert len(calls) == 1

    def test_rejects_nonpositive_attempts(self, tmp_path):
        """attempts < 1 would silently skip the replace; refuse it instead."""
        src = tmp_path / "tmp.tmp"
        src.write_text("payload")
        with pytest.raises(ValueError):
            replace_with_retry(src, tmp_path / "target.json", attempts=0)
        assert src.exists()


class TestReadTextWithRetry:
    """The read side of the same Windows window. `_write_json` publishes the
    roster by renaming onto `sequence.json`, so the file is freshly-modified
    exactly when AV/the indexer opens it — and a strict roster read raises
    ConfigError at ~59 call sites, including SwitchTransaction.rollback."""

    def _win_oserror(self, winerror: int) -> OSError:
        e = PermissionError(13, "Access is denied")
        e.winerror = winerror
        return e

    @pytest.mark.parametrize("winerror", [5, 32, 33])
    def test_retries_transient_windows_errors(self, tmp_path, monkeypatch, winerror):
        monkeypatch.setattr("claude_swap.fsutil.sys.platform", "win32")
        target = tmp_path / "sequence.json"
        target.write_text("payload")
        calls = []
        real_read = Path.read_text
        err = self._win_oserror(winerror)

        def flaky(path_self, *a, **kw):
            calls.append(1)
            if len(calls) < 4:
                raise err
            return real_read(path_self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", flaky)

        assert read_text_with_retry(target) == "payload"
        assert len(calls) == 4

    def test_gives_up_and_raises_after_attempts(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.fsutil.sys.platform", "win32")
        err = self._win_oserror(32)

        def always_fail(self, *a, **kw):
            raise err

        monkeypatch.setattr(Path, "read_text", always_fail)
        with pytest.raises(OSError):
            read_text_with_retry(tmp_path / "a", attempts=3)

    def test_posix_eacces_surfaces_immediately(self, tmp_path, monkeypatch):
        """A POSIX EACCES is a real, persistent permission problem: raise at
        once rather than stalling ~0.75s on a condition that will not clear."""
        monkeypatch.setattr("claude_swap.fsutil.sys.platform", "linux")
        calls = []
        err = self._win_oserror(5)

        def fail(self, *a, **kw):
            calls.append(1)
            raise err

        monkeypatch.setattr(Path, "read_text", fail)
        with pytest.raises(OSError):
            read_text_with_retry(tmp_path / "a")
        assert len(calls) == 1

    def test_does_not_retry_a_non_contention_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.fsutil.sys.platform", "win32")
        calls = []
        err = self._win_oserror(2)  # ERROR_FILE_NOT_FOUND

        def missing(self, *a, **kw):
            calls.append(1)
            raise err

        monkeypatch.setattr(Path, "read_text", missing)
        with pytest.raises(OSError):
            read_text_with_retry(tmp_path / "a")
        assert len(calls) == 1

    def test_rejects_nonpositive_attempts(self, tmp_path):
        target = tmp_path / "a.json"
        target.write_text("x")
        with pytest.raises(ValueError):
            read_text_with_retry(target, attempts=0)


class TestSkipifArgumentsAreEvaluatedEverywhere:
    """A `skipif` ARGUMENT runs at collection on every platform.

    So a POSIX-only call in one decorator is reached even when a second
    decorator below it would skip the test on Windows — and Windows has no
    `os.geteuid`, so the module fails to IMPORT and the whole suite stops:

        collected 1819 items / 1 error
        E   AttributeError: module 'os' has no attribute 'geteuid'
        !!! Interrupted: 1 error during collection !!!

    Nothing in the suite could catch that, because the suite is what dies.
    This asserts the property directly instead: every `skipif` condition in
    this module must be evaluable on a platform where the POSIX-only names
    are absent.
    """

    def test_no_skipif_condition_calls_a_posix_only_name_unguarded(self):
        import ast
        import pathlib

        src = pathlib.Path(__file__).read_text()
        # Names that do not exist on Windows. A call to one of these inside a
        # skipif condition must be short-circuited by a platform test to its
        # LEFT, in the same expression — a separate decorator cannot do it.
        posix_only = {"geteuid", "getuid", "geteguid", "getgid"}
        offenders = []

        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr in posix_only):
                continue
            offenders.append(fn.attr)

        # Every such call must sit inside a BoolOp whose earlier operand tests
        # the platform, so `or`/`and` short-circuits before it is reached.
        guarded = []
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.BoolOp):
                continue
            for i, operand in enumerate(node.values):
                if i == 0:
                    continue
                for sub in ast.walk(operand):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr in posix_only
                    ):
                        earlier = ast.dump(node.values[0])
                        if "platform" in earlier:
                            guarded.append(sub.func.attr)

        assert sorted(offenders) == sorted(guarded), (
            f"POSIX-only calls {offenders} but only {guarded} are behind a "
            "platform short-circuit — an unguarded one is evaluated at "
            "COLLECTION on Windows and takes the whole suite down"
        )


class TestStrictRosterReadBranches:
    """`_read_json(strict=True)`'s OSError and non-dict branches were both
    reachable and both unkilled. `_get_sequence_data` is strict because ~59
    call sites read it and 27 write the result back through `or {}` — a torn
    or unreadable roster read as "no accounts" rebuilds it from nothing."""

    def _switcher(self, tmp_path):
        import logging

        from claude_swap.switcher import ClaudeAccountSwitcher

        s = ClaudeAccountSwitcher.__new__(ClaudeAccountSwitcher)
        s._logger = logging.getLogger("test")
        return s

    def test_a_roster_holding_a_list_is_refused_not_dereferenced(self, tmp_path):
        """Without the guard the caller gets AttributeError: 'list' object has
        no attribute 'get' — uncaught by cli.py's `except ClaudeSwitchError`,
        so `--json` emits no envelope at all."""
        from claude_swap.exceptions import ConfigError

        p = tmp_path / "sequence.json"
        p.write_text("[1, 2, 3]")
        with pytest.raises(ConfigError, match="not a JSON object"):
            self._switcher(tmp_path)._read_json(p, strict=True)

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="POSIX mode semantics; root reads through a 0000 mode",
    )
    def test_an_unreadable_roster_is_refused_not_read_as_empty(self, tmp_path):
        """The distinction the strict reader exists for: `None` here would be
        spliced back as "no accounts" and destroy the roster on the next
        write."""
        from claude_swap.exceptions import ConfigError

        p = tmp_path / "sequence.json"
        p.write_text('{"sequence": [1], "accounts": {"1": {}}}')
        os.chmod(p, 0o000)
        try:
            with pytest.raises(ConfigError, match="could not be read"):
                self._switcher(tmp_path)._read_json(p, strict=True)
        finally:
            os.chmod(p, 0o600)

    def test_non_strict_keeps_the_soft_none(self, tmp_path):
        """Read-only callers that pass no `strict` still get `None`, so this
        change cannot turn an advisory read into a hard failure."""
        p = tmp_path / "config.json"
        p.write_text("[1, 2, 3]")
        assert self._switcher(tmp_path)._read_json(p) is None


class TestAtomicWrite:
    """``atomic_write_text`` is how every published file lands: temp beside
    the target, fsync, rename. The switch rollback restores ``~/.claude.json``
    through it because ``Path.write_text`` truncates the live file before it
    writes, and Claude Code, which watches that file, treats a torn read as a
    corrupted config (#419)."""

    def test_publishes_by_rename(self, tmp_path):
        target = tmp_path / ".claude.json"
        target.write_text("old")
        before = target.stat().st_ino
        atomic_write_text(target, "new")
        assert target.read_text(encoding="utf-8") == "new"
        assert list(tmp_path.iterdir()) == [target]
        if sys.platform != "win32":
            assert target.stat().st_ino != before  # published, not rewritten
            assert target.stat().st_mode & 0o777 == 0o600

    def test_failed_publish_leaves_destination_untouched(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / ".claude.json"
        target.write_text("old")

        def refuse(src, dst, **kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr("claude_swap.fsutil.replace_with_retry", refuse)
        with pytest.raises(OSError):
            atomic_write_text(target, "new")
        assert target.read_text(encoding="utf-8") == "old"
        assert list(tmp_path.iterdir()) == [target]

    def test_interrupted_write_leaves_no_temp(self, tmp_path, monkeypatch):
        """A Ctrl-C is not an OSError; the temp must still go. Injected at
        fsync, the one call in the sequence only this writer makes."""
        target = tmp_path / ".claude.json"
        target.write_text("old")

        def interrupt(fd):
            raise KeyboardInterrupt

        monkeypatch.setattr("claude_swap.fsutil.os.fsync", interrupt)
        with pytest.raises(KeyboardInterrupt):
            atomic_write_text(target, "new")
        assert target.read_text(encoding="utf-8") == "old"
        assert list(tmp_path.iterdir()) == [target]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_temp_is_private_from_creation(self, tmp_path, monkeypatch):
        """With the mode call neutralised the published mode is the one the
        temp was created with: a write-then-chmod writer shows up as 0644."""
        monkeypatch.setattr("claude_swap.fsutil.os.fchmod", lambda *a, **k: None)
        old_umask = os.umask(0o022)
        try:
            atomic_write_text(tmp_path / "secret.json", "{}")
        finally:
            os.umask(old_umask)
        assert (tmp_path / "secret.json").stat().st_mode & 0o777 == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_failing_mode_call_aborts_before_publishing(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "secret.json"
        target.write_text("old")

        def refuse(fd, mode):
            raise PermissionError("fchmod denied (injected)")

        monkeypatch.setattr("claude_swap.fsutil.os.fchmod", refuse)
        with pytest.raises(PermissionError):
            atomic_write_text(target, "new")
        assert target.read_text(encoding="utf-8") == "old"
        assert list(tmp_path.iterdir()) == [target]

    def test_fsync_unsupported_is_tolerated_other_errors_are_not(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "x.json"
        target.write_text("old")

        def unsupported(fd):
            raise OSError(errno.EINVAL, "fsync not supported here")

        monkeypatch.setattr("claude_swap.fsutil.os.fsync", unsupported)
        atomic_write_text(target, "new")
        assert target.read_text(encoding="utf-8") == "new"

        def broken(fd):
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr("claude_swap.fsutil.os.fsync", broken)
        with pytest.raises(OSError):
            atomic_write_text(target, "newer")
        assert target.read_text(encoding="utf-8") == "new"
        assert list(tmp_path.iterdir()) == [target]

    def test_suite_runs_with_fsync_stubbed(self):
        """conftest stubs ``os.fsync``; the real one rejects a bad descriptor."""
        os.fsync(-1)

    def test_atomic_write_with_real_fsync(self, tmp_path, monkeypatch):
        """The stub leaves no other test a real fsync: one on a closed fd
        (EBADF) would pass the whole suite."""
        with pytest.raises(ValueError):
            _REAL_FSYNC(-1)  # the capture is the real function, not the stub
        monkeypatch.setattr(os, "fsync", _REAL_FSYNC)
        atomic_write_text(tmp_path / "x.json", "new")
        assert (tmp_path / "x.json").read_text(encoding="utf-8") == "new"

    def test_missing_parent_is_not_created(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            atomic_write_text(tmp_path / "missing" / "x.json", "{}")
        assert not (tmp_path / "missing").exists()

    def test_bytes_round_trip(self, tmp_path):
        target = tmp_path / "blob.bin"
        payload = bytes(range(256)) * 300
        atomic_write_bytes(target, payload)
        assert target.read_bytes() == payload
        assert list(tmp_path.iterdir()) == [target]


class TestAtomicWriteSymlinks:
    """A rename never follows a link: renaming onto a symlinked path detaches
    it silently (#192). The writer publishes onto the end of the chain, as
    Claude Code's own config writer does for ``~/.claude.json``."""

    def test_writes_through_a_link_and_keeps_it(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        live = tmp_path / "live"
        live.mkdir()
        tracked = repo / ".claude.json"
        tracked.write_text("old")
        link = live / ".claude.json"
        link.symlink_to(tracked)

        atomic_write_text(link, "new")

        assert link.is_symlink(), "the dotfiles link must survive the write"
        assert tracked.read_text(encoding="utf-8") == "new"
        assert [p.name for p in repo.iterdir()] == [".claude.json"]
        assert [p.name for p in live.iterdir()] == [".claude.json"]

    def test_follows_a_relative_two_hop_chain(self, tmp_path):
        (tmp_path / "real").mkdir()
        final = tmp_path / "real" / "final.json"
        final.write_text("old")
        # Native separator: Windows rejects a forward slash inside a reparse
        # point (WinError 123 from realpath), which is the link, not the writer.
        (tmp_path / "hop2").symlink_to(Path("real") / "final.json")
        (tmp_path / "hop1").symlink_to("hop2")

        atomic_write_text(tmp_path / "hop1", "new")

        assert (tmp_path / "hop1").is_symlink()
        assert (tmp_path / "hop2").is_symlink()
        assert final.read_text(encoding="utf-8") == "new"
        assert publish_target(tmp_path / "hop1") == final.resolve()

    @pytest.mark.skipif(
        not hasattr(os.path, "ALLOW_MISSING"),
        reason="pre-3.12.11 resolver refuses a dangling link",
    )
    def test_dangling_link_writes_where_it_points(self, tmp_path):
        (tmp_path / "gone").mkdir()
        link = tmp_path / ".claude.json"
        link.symlink_to(tmp_path / "gone" / "config.json")

        atomic_write_text(link, "{}")

        assert link.is_symlink()
        assert (tmp_path / "gone" / "config.json").read_text(encoding="utf-8") == "{}"
        assert [p.name for p in (tmp_path / "gone").iterdir()] == ["config.json"]

    def test_follow_symlinks_false_replaces_the_link(self, tmp_path):
        target = tmp_path / "elsewhere.json"
        target.write_text("untouched")
        link = tmp_path / "out.cswap"
        link.symlink_to(target)

        atomic_write_text(link, "export", follow_symlinks=False)

        assert not link.is_symlink()
        assert link.read_text(encoding="utf-8") == "export"
        assert target.read_text(encoding="utf-8") == "untouched"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["elsewhere.json", "out.cswap"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink loop semantics")
    def test_loop_raises_eloop_and_writes_nothing(self, tmp_path):
        loop = tmp_path / "loop.json"
        loop.symlink_to("loop.json")

        with pytest.raises(OSError) as info:
            atomic_write_text(loop, "{}")

        assert info.value.errno == errno.ELOOP
        assert loop.is_symlink()
        assert [p.name for p in tmp_path.iterdir()] == ["loop.json"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink loop semantics")
    def test_loop_hidden_behind_dotdot_is_still_refused(self, tmp_path):
        """Non-strict realpath would collapse ``loop/..`` lexically and aim
        the write at ``victim.json``, a file ``open`` cannot even reach."""
        victim = tmp_path / "victim.json"
        victim.write_bytes(b"victim")
        (tmp_path / "loop").symlink_to("loop")
        config = tmp_path / "config.json"
        config.symlink_to("loop/../victim.json")

        with pytest.raises(OSError) as info:
            atomic_write_text(config, "{}")

        assert info.value.errno == errno.ELOOP
        assert victim.read_bytes() == b"victim"
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "config.json", "loop", "victim.json",
        ]

    def test_publish_target_of_a_plain_path_is_the_path(self, tmp_path):
        p = tmp_path / "plain.json"
        assert publish_target(p) == p
        p.write_text("x")
        assert publish_target(p) == p

    def test_strict_fallback_refuses_a_dangling_link(self, tmp_path, monkeypatch):
        """On a 3.12 release without ``ALLOW_MISSING`` the resolver runs
        fully strict: a dangling link is refused rather than guessed, while
        an existing target is still written through."""
        monkeypatch.setattr("claude_swap.fsutil._RESOLVE_STRICT", True)
        (tmp_path / "gone").mkdir()
        dangling = tmp_path / "dangling.json"
        dangling.symlink_to(tmp_path / "gone" / "config.json")

        with pytest.raises(FileNotFoundError):
            atomic_write_text(dangling, "{}")

        assert dangling.is_symlink()
        assert list((tmp_path / "gone").iterdir()) == []
        assert not list(tmp_path.glob("*.tmp"))

        tracked = tmp_path / "tracked.json"
        tracked.write_text("old")
        link = tmp_path / "link.json"
        link.symlink_to(tracked)
        atomic_write_text(link, "new")
        assert link.is_symlink()
        assert tracked.read_text(encoding="utf-8") == "new"


class TestAtomicWriteTempName:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="MAX_PATH turns a 251-character name into a path-length question",
    )
    def test_a_destination_name_near_name_max_still_publishes(self, tmp_path):
        """The temp carries a bounded slice of the destination name: a legal
        251-character destination must not fail with ENAMETOOLONG at the temp
        (mkstemp adds eight random characters and ``.tmp`` to the prefix)."""
        target = tmp_path / ("x" * 245 + ".cswap")
        target.write_text("old")

        atomic_write_text(target, "new")

        assert target.read_text(encoding="utf-8") == "new"
        assert [p.name for p in tmp_path.iterdir()] == [target.name]
