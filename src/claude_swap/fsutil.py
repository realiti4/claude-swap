"""Filesystem primitives with no claude_swap dependencies.

Deliberately a leaf module: the atomic-write helpers here are needed by
settings, credentials, mappings and session, which sit on both sides of
the paths/models/usage_store import cycle.
"""

from __future__ import annotations

import errno
import os
import sys
import tempfile
import time
from pathlib import Path

# Windows error codes that usually mean "someone else has the file open right
# now": ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION and ERROR_LOCK_VIOLATION.
# AV/indexer contention clears within milliseconds; ERROR_ACCESS_DENIED can
# also be a persistent condition (ACLs, read-only attribute), which still
# surfaces once the bounded retries are exhausted.
_TRANSIENT_WIN_ERRORS = frozenset({5, 32, 33})


def read_text_with_retry(
    path: os.PathLike | str,
    *,
    attempts: int = 10,
    initial_delay: float = 0.002,
) -> str:
    """``Path.read_text``, retried past the same transient Windows failures.

    The read side of :func:`replace_with_retry`, and it needs the same
    treatment for the same reason: ``_write_json`` publishes the roster by
    renaming onto ``sequence.json``, so the file is freshly-modified exactly
    when AV/the indexer opens it opportunistically, and a reader arriving in
    that window gets ERROR_SHARING_VIOLATION as a ``PermissionError``.

    That window matters more on the read side than the write side, because a
    strict roster read raises ``ConfigError`` at ~59 call sites — including
    read-only ones (``list_accounts``, ``_account_kind``) and
    ``SwitchTransaction.rollback``, where turning a recoverable switch failure
    into a failed ROLLBACK is the worst outcome available.

    POSIX is untouched: there is no such window, and an ``EACCES`` there is a
    genuine, persistent permission problem that must surface at once rather
    than after a ~0.75 s stall.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    delay = initial_delay
    for attempt in range(attempts):
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError as e:
            transient = (
                sys.platform == "win32"
                and getattr(e, "winerror", None) in _TRANSIENT_WIN_ERRORS
            )
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)
    raise AssertionError("unreachable")  # pragma: no cover - loop always exits


def replace_with_retry(
    src: os.PathLike | str,
    dst: os.PathLike | str,
    *,
    attempts: int = 10,
    initial_delay: float = 0.002,
) -> None:
    """``os.replace``, retried past transient Windows sharing failures.

    POSIX ``rename`` is genuinely atomic and never fails this way, but on
    Windows antivirus (Defender) and the search indexer open freshly-created
    files opportunistically. A replace onto a just-written target then fails
    with ERROR_ACCESS_DENIED or ERROR_SHARING_VIOLATION for a few milliseconds
    — measured at ~44% of replaces into a scanned temp directory, which made
    credential and usage-store writes fail intermittently for real users.

    Only the contention codes are retried; other errors (missing source,
    cross-device link) surface on the first attempt. A *persistent* access
    denial (ACLs, read-only target) matches the retried codes and so raises
    only after the attempt budget — a ~0.75 s delay in the worst case.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    delay = initial_delay
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            transient = (
                sys.platform == "win32"
                and getattr(e, "winerror", None) in _TRANSIENT_WIN_ERRORS
            )
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


# Filesystems that do not implement fsync answer one of these; the write goes
# ahead without the durability it would otherwise buy (Claude Code's own
# writer tolerates the same set). Anything else -- EIO above all -- raises.
_FSYNC_UNSUPPORTED = frozenset(
    code
    for code in (
        getattr(errno, name, None)
        for name in ("EINVAL", "ENOTSUP", "EOPNOTSUPP", "ENOSYS")
    )
    if code is not None
)


# ``os.path.ALLOW_MISSING`` (3.12.11 / 3.13.4 and later) resolves a symlink
# chain strictly -- a loop or any other resolution error raises -- while
# allowing the final referent to be missing. Older patch releases fall back to
# fully strict resolution, which refuses a dangling link rather than guess.
# Module-level so a test can pin the fallback without touching ``os.path``.
_RESOLVE_STRICT = getattr(os.path, "ALLOW_MISSING", True)

# NAME_MAX is 255 bytes on every filesystem cswap meets, and ``mkstemp`` adds
# eight random characters plus ``.tmp`` to the prefix. A prefix carrying the
# whole destination name would push a legal 243+ character destination over
# the limit -- ENAMETOOLONG at the temp, for a file that itself fits. Keep
# the association readable for ordinary names and bounded for long ones.
_TEMP_PREFIX_CHARS = 40


def publish_target(path: os.PathLike | str) -> Path:
    """Where a publish to ``path`` lands: ``path`` itself or, for a symlink,
    the end of its chain.

    Written THROUGH a link rather than over it: a rename swaps a directory
    entry and never follows links, so renaming onto a symlinked path detaches
    it -- the write succeeds, the content is right, and the link's target
    silently stops receiving updates (#192). Claude Code's own config writer
    writes through a symlinked ``~/.claude.json`` for the same reason.

    Resolution is ``os.path.realpath`` with ``strict=os.path.ALLOW_MISSING``:
    the whole chain (not one hop), a missing final referent allowed (a
    dangling link writes where it points), a loop or any other resolution
    error raised. Not the non-strict default: that collapses
    ``loop/../victim.json`` lexically into ``victim.json`` and would aim the
    write at a different file than ``open`` reaches (which fails with ELOOP).
    """
    target = Path(path)
    if not target.is_symlink():
        return target
    return Path(os.path.realpath(target, strict=_RESOLVE_STRICT))


def atomic_write_bytes(
    path: os.PathLike | str,
    data: bytes,
    *,
    mode: int = 0o600,
    follow_symlinks: bool = True,
) -> None:
    """Publish ``data`` at ``path`` by temp file + rename.

    ``mkstemp`` creates the temp beside the target (0600 from creation, same
    filesystem, so the rename is atomic); the bytes are written, ``mode`` is
    set on the open descriptor, the content is fsynced, and the temp is
    renamed over the target. A reader sees the old file or the new one, never
    a truncated or partial one -- which is what ``Path.write_text`` (O_TRUNC,
    then write) cannot promise, and why the switch rollback must not use it:
    Claude Code watches ``~/.claude.json`` and treats a torn read as a
    corrupted config (#419).

    ``follow_symlinks`` (the default) publishes onto the end of a symlink
    chain so the link survives -- see :func:`publish_target`. Pass ``False``
    for a destination the user typed, such as an export file under ``/tmp``,
    where a link planted by someone else must not redirect the write: the
    rename then replaces the link itself.

    Precisely what is promised: a failure before the rename leaves the
    destination exactly as it was and removes the temp; no further ordinary
    fallible operation follows the rename (a signal can still interrupt the
    caller once the file is published). The content is fsynced before it is
    published, so a crash cannot leave an empty new file where an old one was.
    The durability of the rename itself is the directory's business and is
    not promised: a crash right after it can lose a newly created file or
    show the previous version of an existing one. Where the filesystem
    reports fsync as unsupported, the write proceeds without that guarantee.
    One cleanup gap is accepted: a signal landing inside ``mkstemp`` between
    the create and the return of its name strands one 0600 temp.

    Not done here on purpose: no ``mkdir`` of parents (callers that need it do
    it first), and no in-place fallback when the rename is refused -- a
    bind-mounted target answers EBUSY, the write fails, the destination is
    untouched. Claude Code's own writer does fall back to an in-place rewrite
    there (keeping its temp until the rewrite lands); that is the shape to copy
    if anyone ever asks for it, in this one place.
    """
    target = publish_target(path) if follow_symlinks else Path(path)
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f"{target.name[:_TEMP_PREFIX_CHARS]}.",
        suffix=".tmp",
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError(errno.EIO, "write made no progress", tmp)
            view = view[written:]
        # Windows before 3.13 has no fchmod; the attribute check (not only the
        # platform string) keeps this safe under tests that patch sys.platform
        # to darwin process-wide, as the launch agent tests do.
        if sys.platform != "win32" and hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        try:
            os.fsync(fd)
        except OSError as e:
            if e.errno not in _FSYNC_UNSUPPORTED:
                raise
        os.close(fd)
        fd = -1
        replace_with_retry(tmp, target)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(
    path: os.PathLike | str,
    text: str,
    *,
    mode: int = 0o600,
    follow_symlinks: bool = True,
) -> None:
    """:func:`atomic_write_bytes` for UTF-8 text."""
    atomic_write_bytes(
        path, text.encode("utf-8"), mode=mode, follow_symlinks=follow_symlinks
    )


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------
#
# A link, here, is a directory entry that leads somewhere else and holds no
# data of its own: a symlink on any platform, or a directory junction on
# Windows. Junctions matter because they are the one directory link an
# ordinary Windows user can create (a symlink needs Developer Mode or an
# elevated token), and because the stdlib does not count them as links:
# ``Path.is_symlink()`` is False for one, ``is_dir()`` follows it, and
# ``rglob`` and ``os.walk`` descend into it as into a plain directory. Code
# that must tell a cswap-made link from real user data goes through
# ``is_link``, never ``is_symlink``.

# Windows stores a link target with the extended-length prefix in front:
# ``\\?\C:\...``, or ``\\?\UNC\server\share\...`` for a network path.
_EXTENDED_PREFIX = "\\\\?\\"
_EXTENDED_UNC_PREFIX = _EXTENDED_PREFIX + "UNC\\"


def is_link(path: os.PathLike | str) -> bool:
    """Whether ``path`` is a symlink or a Windows junction, dangling or not."""
    target = Path(path)
    try:
        return target.is_symlink() or target.is_junction()
    except OSError:
        return False


def _without_extended_prefix(path: str) -> str:
    """Drop the extended-length prefix Windows puts on a stored link target."""
    if path.startswith(_EXTENDED_UNC_PREFIX):
        return "\\\\" + path[len(_EXTENDED_UNC_PREFIX):]
    if path.startswith(_EXTENDED_PREFIX):
        return path[len(_EXTENDED_PREFIX):]
    return path


def link_points_at(link: os.PathLike | str, target: os.PathLike | str) -> bool:
    """Whether the link at ``link`` stores ``target`` as its destination.

    Compares the stored path, not where it leads: a link to an intermediate
    symlink is a different (and worse) link than one to the resolved file
    even though both end at the same place -- see the note on dotfiles
    setups in ``session.SessionManager._sync_items``. Windows hands a stored
    path back with its extended-length prefix, which is stripped first.
    False for anything that is not a link.
    """
    try:
        stored = os.readlink(link)
    except OSError:
        return False
    return Path(_without_extended_prefix(os.fspath(stored))) == Path(target)


def remove_link(link: os.PathLike | str) -> None:
    """Remove a symlink or junction itself, never what it leads to.

    ``os.unlink`` takes a file symlink anywhere, a directory symlink on
    POSIX, and on Windows a directory symlink and a junction alike (CPython
    removes a directory reparse point with ``RemoveDirectoryW``). ``rmdir``
    is the fallback for a build that answers EISDIR or EPERM instead.
    """
    try:
        os.unlink(link)
    except (IsADirectoryError, PermissionError):
        os.rmdir(link)


def make_dir_link(target: os.PathLike | str, link: os.PathLike | str) -> None:
    """Put a directory link at ``link`` leading to ``target``.

    A symlink on POSIX. On Windows a junction: it needs no privilege, stores
    an absolute path, and Claude Code reads and writes through it exactly as
    through a symlink (issue #292). ``target`` must be absolute and on a
    local volume; a filesystem without reparse points (FAT, a network
    share) refuses with OSError -- ``can_junction`` says so in advance.
    """
    if sys.platform == "win32":
        import _winapi  # Windows-only stdlib module; private but stable since 3.5

        _winapi.CreateJunction(os.fspath(target), os.fspath(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def make_file_link(target: os.PathLike | str, link: os.PathLike | str) -> None:
    """Put a symlink at ``link`` leading to the file ``target``.

    The one link cswap makes that needs a privilege on Windows: Developer
    Mode, or an elevated token. ``can_symlink`` says so in advance.
    """
    os.symlink(target, link)


# ``(directory, kind) -> bool``: each probe runs once per process. Module-level
# so a test can reset it.
_LINK_PROBES: dict[tuple[str, str], bool] = {}


def _probe_link(directory: os.PathLike | str, kind: str, make) -> bool:
    key = (os.path.normcase(os.path.abspath(directory)), kind)
    if key not in _LINK_PROBES:
        probe = Path(directory) / f".cswap-link-probe-{os.getpid()}"
        try:
            try:
                os.unlink(probe)  # left by a probe that was interrupted
            except OSError:
                pass
            make(probe)
            os.unlink(probe)
            _LINK_PROBES[key] = True
        except OSError:
            try:
                os.unlink(probe)
            except OSError:
                pass
            _LINK_PROBES[key] = False
    return _LINK_PROBES[key]


def can_symlink(directory: os.PathLike | str) -> bool:
    """Whether this process may create a file symlink in ``directory``.

    Always true off Windows, where trying is the check. On Windows only a
    process with Developer Mode on or an elevated token may, so one dangling
    probe link is made and removed, once per directory per process (the
    answer cannot change within one). Probed in the directory the real link
    goes in, so a filesystem or permission difference cannot say yes here
    and no there.
    """
    if sys.platform != "win32":
        return True
    return _probe_link(
        directory, "symlink", lambda probe: os.symlink("cswap-link-probe", probe)
    )


def can_junction(
    directory: os.PathLike | str, target: os.PathLike | str | None = None
) -> bool:
    """Whether a directory link from ``directory`` to ``target`` can be made.

    Always true off Windows (a symlink). A junction needs no privilege but
    does need a volume with reparse points -- NTFS or ReFS; FAT and network
    shares refuse -- and a target Windows accepts, so one is made from
    ``directory`` to ``target`` (``directory`` itself when none is given)
    and removed, once per directory and target per process. Pass the parent
    of the real targets, so the probe fails where the real link would.
    """
    if sys.platform != "win32":
        return True
    to = os.path.abspath(target if target is not None else directory)
    return _probe_link(
        directory,
        f"junction:{os.path.normcase(to)}",
        lambda probe: make_dir_link(to, probe),
    )
