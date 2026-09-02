"""Cooperate with Claude Code's own advisory locks while mutating its files.

Claude Code guards its OAuth token refresh with the npm ``proper-lockfile``
package, and its ``~/.claude.json`` writes with the same mechanism on the
config file. The protocol (verified against the 2.1.218 bundle):

- The lock artifact is a **directory**; ``mkdir`` atomicity is the mutex.
- The refresh path takes **two** locks, in order: the primary
  ``<config-home>/.oauth_refresh.lock``, then the legacy
  ``<config-home>.lock`` (``~/.claude.lock``) kept for compatibility with
  external tools. Both run ``stale: 60000, update: 5000`` — a credential
  lock is stale only past **60s**, and live holders touch every 5s. On a
  contended legacy lock Claude Code releases the primary and retries.
- The config lock (``~/.claude.json.lock``) keeps the older defaults:
  stale after 10s, touched every 5s.
- Claude Code retries a held credentials lock 5 times with 1-2s jittered
  sleeps before giving up, so briefly holding it is fully cooperative.

Holding these locks while swapping credentials closes the one real race with a
running Claude Code: its refresh reads credentials, refreshes over the network,
and saves — all under both credential locks — so a swap landing inside that
window would be overwritten by the refreshed old-account token (and the
just-taken backup would keep a pre-rotation refresh token). Under the lock,
Claude Code's own double-checked re-read sees the swapped (non-expired)
credential and aborts the refresh instead.

References (claude-code 2.1.218 bundle): the ``uKi`` lock-options helper
(``lockfilePath: join(dir, ".oauth_refresh.lock"), stale: 60000, update:
5000``) and ``CKi`` (dual acquisition, legacy released-on-contention with
``tengu_oauth_refresh_legacy_lock_contended`` telemetry).
"""

from __future__ import annotations

import logging
import os
import random
import stat
import sys
import threading
import tempfile
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

from claude_swap.exceptions import ClaudeCodeLockTimeout
from claude_swap.paths import get_claude_config_home, get_global_config_path

# Claude Code's credential-refresh locks run ``stale: 60000, update: 5000``
# (2.1.218 ``uKi``): a lock younger than 60s belongs to a live holder and
# must never be stolen — the holder's toucher may stall well past 10s
# (suspend, blocked event loop) while it still legitimately owns the lock.
CREDENTIALS_STALENESS_S = 60.0
# The config lock (~/.claude.json.lock) keeps the older proper-lockfile
# defaults: stale after 10s, touched every 5s.
CONFIG_STALENESS_S = 10.0
# We touch a little faster than CC's 5s for margin.
TOUCH_INTERVAL_S = 3.0
# How long the release waits for a heartbeat tick that is holding the stamp
# mutex. PER-LOCK, and a switch takes three. Unpinned platforms only: with a
# descriptor pinning the inode there is no mutex and no wait.
_RELEASE_WAIT_S = 5.0
# WINDOWS CANNOT HOLD A DIRECTORY OPEN: `os.open` on one raises EACCES, and
# the stdlib offers no other way. Identity there is the file index from a
# plain stat -- still not a value we write, so still not adoptable the way an
# mtime stamp was, but not pinned, so an index a takeover frees can come back.
_CAN_PIN_A_DIRECTORY = sys.platform != "win32"

# Keyed on the DEVICE the mkdir lands on. One probe per filesystem per
# process; a stray answer is never cached.
_PIN_PROBE: dict[int, bool] = {}
_PIN_TRIALS = 4
# Repeats bound the error only if the samples are INDEPENDENT. Trials
# microseconds apart fit inside one contention burst and agree wrongly, and a
# gap crosses it. Under CONTINUOUS saturation the gap buys nothing and only the
# count does, so this is not a p**n error bound in either direction.
_PIN_TRIAL_GAP_S = 0.010


def _fd_pins_an_inode(parent: Path) -> bool:
    """Whether an open descriptor stops the next ``mkdir`` here reusing the
    inode of a directory that was removed under it.

    A FILESYSTEM property, not a platform one, and the release's whole
    ownership guard rests on it: an unheld inode number is reused by the
    next mkdir, while an open fd pins it in the orphan list. A network
    filesystem has no server-side open state to hold that list, so the fd
    pins nothing, `(st_dev, st_ino)` matches a stranger's directory, and
    the release removes a live successor's lock. Measured on NFSv3: 200 of
    200 trials reused the number with the descriptor still open, against 0
    of 200 on ext4 and overlayfs, with the no-descriptor control at 200 of
    200 on all three.

    False when the probe cannot run. Refusing to trust the pin arms the
    stamp, the `unproven` latch and the release mutex, which costs a lock
    left for the stale sweep; trusting it wrongly costs a peer its lock.
    """
    if not _CAN_PIN_A_DIRECTORY:
        return False
    # THE DEVICE IS THE WHOLE SUBJECT: pinning is a property of the mount, not
    # of a path on it, so a mount landing here is a miss (which is the point)
    # and a second parent on the same filesystem is not (which the path cost).
    try:
        key = os.stat(parent).st_dev
    except OSError:
        return False
    cached = _PIN_PROBE.get(key)
    if cached is not None:
        return cached
    # THE SAFE ANSWER WINS, AND ONE TRIAL CANNOT GIVE IT. A trial reports
    # "pinned" by NOT seeing the number come back, so any concurrent
    # allocation in this filesystem during the rmdir/mkdir window reads as
    # a pin -- the direction that disarms the stamp, the `unproven` latch
    # and the release mutex, cached for the life of the process. One trial
    # that DOES see the reuse is proof it does not pin; no number of trials
    # can prove that it does, so repeat and let a single "reused" decide.
    for i in range(_PIN_TRIALS):
        if i:
            time.sleep(_PIN_TRIAL_GAP_S)
        seen = _one_pin_trial(parent)
        if seen is None:
            return False          # cannot run; never cached
        if not seen:
            _PIN_PROBE[key] = False
            return False
    _PIN_PROBE[key] = True
    return True


def _one_pin_trial(parent: Path) -> bool | None:
    """One mkdir/open/rmdir/mkdir cycle. ``None`` when it could not run."""
    fd = -1
    probe = None
    try:
        probe = tempfile.mkdtemp(dir=parent, prefix=".pin-probe-")
        first = os.stat(probe).st_ino
        fd = os.open(probe, os.O_RDONLY)
        os.rmdir(probe)
        os.mkdir(probe)
        return os.stat(probe).st_ino != first
    except OSError:
        return None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if probe is not None:
            try:
                os.rmdir(probe)
            except OSError:
                pass


def _quantum_for_heartbeat(directory: Path) -> int:
    """`_mtime_quantum_ns` for a caller that must not be ended by it.

    The heartbeat runs on a daemon thread, so anything the probe raises there
    kills `_touch` -- and an mtime that stops advancing is the stale lock this
    module exists to prevent, which is worse than any coarsening the quantum
    was added to survive. Unmeasurable therefore means the strict comparison,
    the same answer the probe's own unreadable arm gives.

    `BaseException`, because a non-`OSError` is exactly what escapes the
    probe's internal `except OSError`.
    """
    try:
        return _mtime_quantum_ns(directory)
    except BaseException:
        return 1


def _mtime_quantum_ns(directory: Path) -> int:
    """The finest granularity from a fixed candidate set that this filesystem
    round-trips, in nanoseconds -- or `1`, meaning DO NOT QUANTISE, when
    nothing round-trips or the probe cannot run.

    That `1` is deliberately finer than the truth. On a filesystem coarser
    than every candidate it makes every read-back differ, so `unproven`
    latches and the release leaves the lock for the stale sweep. The
    alternative -- answering with the coarsest candidate -- lets a stamp
    round-trip on the ticks that land on a multiple, and the strict
    comparison then runs against an mtime a successor's `mkdir` truncates to
    that same value. Measured, that removed a live successor's lock in about
    half of the trials.

    Unpinned, the heartbeat proves a hold by writing a stamp and reading it
    back unchanged. A filesystem coarser than the stamp truncates it, so the
    read-back NEVER matches, `unproven` latches on the first tick and the
    release removes nothing -- on an undisturbed hold nobody is contending
    for. Quantising the stamp to what the filesystem keeps makes a mismatch
    mean the one thing it claims to mean: somebody else wrote.

    ponytail: a takeover landing inside a single quantum then reads as our
    own write. On an exact filesystem the quantum is 1 and nothing changes
    at all; on a coarse one this IS the never-releasing branch, and that is
    the trade: short holds lose their release (the stale sweep recovers
    them, with a warning already logged) so that no hold can remove a
    stranger's lock.
    """
    # UNIQUE PER CALL, NOT PER PROCESS. A switch holds three locks at once and
    # at least two of them share a parent, so with the probe moved onto the
    # heartbeat thread two of them measure the SAME path concurrently: the
    # writes leapfrog and the answer comes back FINER than the truth -- the
    # common direction, and the one that latches `unproven` -- or coarser, or
    # one thread's cleanup makes the other's stat ENOENT. Every outcome ends
    # at a lock left on disk for the full staleness window.
    probe = None
    try:
        # INSIDE THE `try`, so the documented `return 1` covers a directory
        # we cannot write. Outside it, a refused create raises out of a
        # function whose only production caller catches `BaseException` by
        # luck and whose test callers do not.
        fd, name = tempfile.mkstemp(dir=directory, prefix=".mtime-probe-")
        os.close(fd)
        probe = Path(name)
        for quantum in (1, 100, 1_000, 1_000_000, 1_000_000_000, 2_000_000_000):
            # AN ODD MULTIPLE, so a coarser filesystem MUST truncate it.
            # `(t // q) * q` is already a multiple of every coarser quantum
            # whenever the clock sits on one, so on a two-second mount the
            # one-second candidate round-tripped for every even second and the
            # answer came back half what it is -- the stamp is then finer than
            # the filesystem keeps and every tick latches `unproven`.
            want = ((time.time_ns() // (2 * quantum)) * 2 + 1) * quantum
            os.utime(probe, ns=(want, want))
            if os.stat(probe).st_mtime_ns == want:
                return quantum
        # NOTHING ROUND-TRIPPED, so the quantum is UNMEASURED -- and the arm
        # that refuses is the safe one. `unproven` is all that stands between
        # a coarse filesystem and `os.rmdir(lock_dir)`: answer with the
        # coarsest candidate and a stamp round-trips on the ticks that land on
        # a multiple, leaving `unproven` clear so the strict comparison runs
        # against an mtime a successor's `mkdir` truncates to that same value.
        # Answering 1 makes every read-back differ, so `unproven` latches and
        # the lock is left for the stale sweep.
        #
        # AT THE NOMINAL INTERVAL, three consecutive ticks cannot all be
        # multiples of the coarsest candidate, so past two ticks the release
        # is refused either way. Off-nominal they can -- and then the coarse
        # answer never latches at all, which is worse. Either way what the
        # coarse answer buys is bounded to the first two ticks, against a
        # collision that is not.
        return 1
    except OSError:
        return 1  # cannot measure; the strict comparison is the old behaviour
    finally:
        try:
            if probe is not None:
                probe.unlink()
        except OSError:
            pass
# Claude Code holds the credentials lock for one token-endpoint round trip
# (sub-second to a few seconds); its config lock for a local RMW. 9s of
# bounded waiting comfortably outlasts both without stalling the CLI forever.
# Note this is a PER-LOCK budget: claude_credentials_lock acquires two locks
# sequentially, so its worst case is ~2x this value.
DEFAULT_TIMEOUT_S = 9.0

_logger = logging.getLogger("claude-swap")


def credentials_lock_dir() -> Path:
    """Legacy credential lock (``~/.claude.lock``) — CC still takes it for
    compatibility; external exclusion today rests on this one."""
    home = get_claude_config_home()
    return home.parent / (home.name + ".lock")


def oauth_refresh_lock_dir() -> Path:
    """Claude Code's primary OAuth refresh lock
    (``<config-home>/.oauth_refresh.lock``, 2.1.218+)."""
    return get_claude_config_home() / ".oauth_refresh.lock"


def config_lock_dir() -> Path:
    """Lock directory guarding the global config file (``~/.claude.json.lock``)."""
    path = get_global_config_path()
    return path.parent / (path.name + ".lock")


@contextmanager
def proper_lockfile(
    lock_dir: Path,
    *,
    timeout: float | None = None,
    staleness: float = CONFIG_STALENESS_S,
):
    """Acquire a proper-lockfile-compatible directory lock.

    Blocks up to ``timeout`` seconds (default ``DEFAULT_TIMEOUT_S``, resolved
    at call time so tests can shorten it), taking over locks whose mtime is
    older than ``staleness``, touches the directory mtime while held so other
    holders don't deem us stale, and removes it on exit — unless it was taken
    over meanwhile, in which case the successor's lock is left in place.

    Raises:
        ClaudeCodeLockTimeout: The lock stayed held past ``timeout``.
        FileNotFoundError: The lock's PARENT is gone. Retrying cannot make a
            directory under a directory nobody has, so that errno is the one
            case this re-raises instead of waiting out the budget.
        OSError: The identity read of a directory we did create failed. A
            waiter cannot tell whose lock it is holding open, so the acquire
            refuses rather than proceed blind.
    """
    if timeout is None:
        timeout = DEFAULT_TIMEOUT_S
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    # PROBED BELOW, on the path that reads it: every reader of `can_pin` is
    # downstream of a `mkdir` that SUCCEEDED, and the probe sleeps between
    # its trials, so above the loop a waiter pays for an answer it never asks.
    can_pin = False
    held_fd = -1
    start = time.monotonic()
    while True:
        try:
            os.mkdir(lock_dir)
            try:
                can_pin = _fd_pins_an_inode(lock_dir.parent)
            except BaseException:
                # THE NAME IS OURS AND THE `finally` THAT REMOVES IT IS NOT
                # REACHED UNTIL THE `yield`. Only `OSError` reaches the arm
                # below, and the probe sleeps -- where an interrupt lands. It
                # never raises `OSError` (every level catches), or both arms
                # would remove one name with a waiter free to take it between.
                try:
                    os.rmdir(lock_dir)
                except OSError:
                    pass
                raise
            # HOLD A DESCRIPTOR ON THE DIRECTORY WE MADE, and take identity
            # from it. An mtime is a value we WRITE, so a successor's
            # directory can come to carry ours; (st_dev, st_ino) is the object
            # itself. The open is what makes it decisive -- an unheld inode
            # number is reused by the next mkdir, while an open fd pins it in
            # the orphan list so mkdir cannot get it back.
            #
            # Open HERE, not after the loop. A waiter that judged this lock
            # stale does stat-then-rmdir, and the holder can release and we
            # can take the name in that gap -- its rmdir then removes the
            # directory we just made. Outside the loop that raises
            # FileNotFoundError out of a call documented to raise only
            # ClaudeCodeLockTimeout.
            try:
                if can_pin:
                    held_fd = os.open(lock_dir, os.O_RDONLY)
                    st = os.fstat(held_fd)
                else:
                    st = os.stat(lock_dir)
            except BaseException as exc:
                # THE DESCRIPTOR IS OURS THE INSTANT `open` RETURNS, and the
                # `finally` that closes it is not reached until the `yield`
                # below. A raise here strands it on an inode the arm further
                # down then unlinks, so it stays pinned for the life of the
                # process.
                if held_fd >= 0:
                    os.close(held_fd)
                    held_fd = -1
                # AND THE NAME IS OURS TOO. An `OSError` is removed by the arm
                # below and must not be removed twice -- the second `rmdir`
                # takes a successor's directory. Anything else reaches no arm
                # at all, so this is the only place that can free it, which is
                # the same window the probe guard above covers.
                if not isinstance(exc, OSError):
                    try:
                        os.rmdir(lock_dir)
                    except OSError:
                        pass
                raise
            ident = (st.st_dev, st.st_ino)
            last_stamp = st.st_mtime_ns
            break
        except FileExistsError:
            pass
        except FileNotFoundError:
            # ONLY A SWEPT NAME RETRIES. The same errno arrives when the
            # PARENT is gone -- removed or replaced after the `parents=True`
            # above -- and that can never succeed, so the fall-through became a
            # full-budget 100%-CPU spin (measured ~95,000 mkdir/s) ending in a
            # timeout that blames Claude Code for a directory nobody has.
            # The parent existing is what separates them, and it is one stat.
            if not lock_dir.parent.is_dir():
                raise
            # Swept between the two calls. Fall THROUGH to the deadline below,
            # never back to the top: a name swept on every attempt has to end
            # at the budget rather than spin until the sweeper stops.
            #
            # AND WAIT ON THE WAY. The fall-through reaches the deadline, then
            # stats a name that is gone and `continue`s, so it never reaches
            # the jittered sleep at the bottom -- ending at the budget while
            # pinning a core for all of it. Never past what is left of the
            # caller's budget, which is the whole loop's contract -- and no
            # claim here about which OTHER arm sleeps, because that sentence
            # has been true and false in successive merges.
            time.sleep(max(0.0, min(0.05, timeout - (time.monotonic() - start))))
        except OSError:
            # `mkdir` made the directory and the open could not read it back,
            # so nobody holds it and every waiter is blocked for the full
            # staleness window. Retrying cannot clear this errno; releasing
            # the name can.
            try:
                os.rmdir(lock_dir)
            except OSError:
                pass
            raise
        # A NAME THAT IS NOT A DIRECTORY IS NEVER A LOCK AND NO RETRY MAKES
        # IT ONE: `mkdir` cannot replace it and `rmdir` answers ENOTDIR, so not
        # even the stale branch clears it. `islink` covered only half of that --
        # a plain file answers a SUCCESSFUL stat, so it burned the whole budget
        # and then blamed Claude Code. `lstat`, so a symlink is judged as itself
        # and not as its target, and every arm above falls through to here.
        # ENOENT means swept between our mkdir and this, a busy handoff that
        # must keep retrying, so an unreadable name retries as it always did.
        try:
            mode = os.lstat(lock_dir).st_mode
        except OSError:
            mode = None
        if mode is not None and not stat.S_ISDIR(mode):
            what = "a symlink" if stat.S_ISLNK(mode) else "not a directory"
            _logger.warning(
                "Lock %s is %s, so no lock can be created there", lock_dir, what
            )
            raise ClaudeCodeLockTimeout(
                f"{lock_dir} is {what}, so no lock can be created there. "
                "Remove it."
            )
        if time.monotonic() - start > timeout:
            raise ClaudeCodeLockTimeout(
                f"Could not acquire {lock_dir.name} — Claude Code appears "
                "to be refreshing credentials. Retry in a few seconds."
            )
        try:
            held_mtime = os.stat(lock_dir).st_mtime
        except FileNotFoundError:
            time.sleep(max(0.0, min(0.05, timeout - (time.monotonic() - start))))
            continue  # holder released between mkdir and stat
        if time.time() - held_mtime > staleness:
            # Dead holder per the protocol: remove and retake. Losing the
            # rmdir/mkdir race to another waiter just means looping again.
            try:
                os.rmdir(lock_dir)
            except OSError:
                time.sleep(0.05)  # can't remove it either; don't spin hot
            continue
        time.sleep(0.25 + random.random() * 0.25)

    stop_touching = threading.Event()
    # PINNED PLATFORMS HAVE NO MUTEX AT ALL: identity is immutable, so the
    # release needs nothing from the heartbeat. Unpinned, `_touch` holds this
    # across three syscalls, so the release's wait for it MUST be bounded --
    # a stalled `utime` would otherwise hold a `finally` that a single switch
    # reaches three times.
    adopt_stamp = False
    # SET BY ANY TICK THAT PROCEEDED WITHOUT EXACT EQUALITY, and never
    # cleared. `os.utime` writes to the NAME and the read-back reads from the
    # NAME, so one lenient tick makes `last_stamp` equal whatever is at that
    # path -- and the release's strict comparison then matches a SUCCESSOR's
    # directory and removes it. A separate reading cannot fix that, because
    # both readings share the value. Only never having accepted an inexact
    # one can, so the release requires this to still be False.
    unproven = False
    # MEASURED IN THE HEARTBEAT, NOT HERE. The probe does filesystem I/O --
    # up to fourteen syscalls -- and this line sits between the acquire's
    # `break` and the `try` whose `finally` removes the lock, the same
    # unprotected gap the thread start below was moved out of. Anything it
    # raises that is not an `OSError` strands the directory with nobody
    # holding it. Its only reader is `_touch`, which runs inside that `try`.
    stamp_quantum: int | None = 1 if can_pin else None
    stamping = None if can_pin else threading.Lock()
    _tick_guard = nullcontext() if stamping is None else stamping

    def _ours(*, strict: bool) -> bool:
        """Is the directory at this path still the one we created?

        Raises whatever the stat raises; each caller decides what an errno
        means for it.

        Pinned, the descriptor settles it and `strict` changes nothing.

        UNPINNED, THE TWO CALLERS WANT OPPOSITE THINGS OF THE MTIME, and
        cannot both be served: an mtime that moved BACKWARDS is either our own
        lock rewound by an external writer (`rsync --times`, a restore, a
        clock step) or a successor stamped in the past, and neither the stamp
        nor a reusable inode number separates them. So each caller gets the
        reading whose mistake is the cheaper one:

        - the heartbeat is LENIENT: only a stamp LATER than our last write can
          be a real takeover, since a takeover's `mkdir` stamps NOW. Reading a
          rewind as theft ends the heartbeat on a lock nobody took, and THAT
          is what lets it go stale and really be taken.
        - the release is STRICT: any stamp but the one we wrote means we
          cannot prove the directory is ours, so we leave it. Its mistake
          costs a lock left for the stale sweep; the other direction removes a
          successor's lock inside its critical section.
        """
        nonlocal adopt_stamp, last_stamp, unproven
        st = os.stat(lock_dir)
        if (st.st_dev, st.st_ino) != ident:
            return False
        if can_pin:
            return True
        if st.st_mtime_ns == last_stamp:
            return True
        if strict:
            return False
        # EVERYTHING BELOW KEEPS THE HEARTBEAT ALIVE WITHOUT PROVING THE
        # DIRECTORY IS OURS, so the release may no longer remove it. A
        # rewind, and a stamp adopted after a failed read-back, are both
        # states where refusing would end the heartbeat on a lock nobody
        # took -- which is how it goes stale and really is taken.
        if adopt_stamp:
            last_stamp, adopt_stamp = st.st_mtime_ns, False
            unproven = True
            return True
        if st.st_mtime_ns < last_stamp:
            unproven = True
            return True
        return False

    def _touch() -> None:
        nonlocal adopt_stamp, last_stamp, stamp_quantum, unproven
        if stamp_quantum is None:
            stamp_quantum = _quantum_for_heartbeat(lock_dir.parent)
        # ABSENCE IS TERMINAL; EVERY OTHER ERRNO IS TRANSIENT. One `except
        # OSError: return` over both syscalls meant a single EIO or ESTALE --
        # the ordinary errnos on a network `~/.claude` -- ended the heartbeat
        # for the rest of the hold, and a mtime that stops advancing is a lock
        # a waiter may take over as stale, mid-swap.
        while not stop_touching.wait(TOUCH_INTERVAL_S):
            with _tick_guard:
                try:
                    if not _ours(strict=False):
                        return  # taken over; refreshing it keeps THEIR lock alive
                except FileNotFoundError:
                    return  # gone; nothing left to keep alive
                except OSError:
                    # UNREADABLE THIS TICK, NOT STOLEN. The refresh below
                    # still runs, because a stat that fails leaves the mtime
                    # where it was and a run of them is the frozen heartbeat
                    # a waiter takes over mid-swap. It is not proof, though.
                    #
                    # ONLY WHERE A STAMP IS CONSULTED. Pinned, identity comes
                    # from the held descriptor and no mtime is read at all, so
                    # recording an unprovable stamp there forfeits the release
                    # over a witness that platform never uses -- one transient
                    # errno left the credentials lock on disk for the whole
                    # staleness window, blocking Claude Code's own refresh.
                    if not can_pin:
                        unproven = True
                # A STAMP WE CHOOSE, so the read-back can be CHECKED. With
                # a bare `utime` the value is "now", nobody can predict it,
                # and the read-back then takes whatever is at the name --
                # which is a SUCCESSOR's mtime when a takeover lands between
                # the identity check and here. That is the one write that
                # made the release's own comparison agree with a stranger.
                stamp = (time.time_ns() // stamp_quantum) * stamp_quantum
                try:
                    os.utime(lock_dir, ns=(stamp, stamp))
                except FileNotFoundError:
                    return
                except OSError:
                    continue  # transient; the next tick refreshes it
                if not can_pin:
                    try:
                        seen = os.stat(lock_dir).st_mtime_ns
                    except OSError:
                        # Our own write landed and we could not read what it
                        # wrote. Adopt the next tick's rather than condemn it
                        # -- but never let the release act on it.
                        adopt_stamp = True
                        unproven = True
                    else:
                        if seen == stamp:
                            last_stamp = stamp
                        else:
                            # Somebody replaced the directory under us. The
                            # stamp is quantised to what this filesystem
                            # keeps, so coarsening can no longer produce this
                            # -- keep beating, remove nothing.
                            last_stamp = seen
                            unproven = True

    toucher = threading.Thread(target=_touch, daemon=True)
    try:
        # STARTED INSIDE THE `try`. A `RuntimeError: can't start new thread`
        # here left the descriptor open AND the lock directory on disk, with
        # nobody holding it -- on a credentials lock that blocks Claude
        # Code's own refresh for the full staleness window.
        toucher.start()
        yield
    finally:
        stop_touching.set()
        # NO WAIT AT ALL WHEN PINNED: a tick landing inside this block can at
        # worst refresh a successor's lock once, and can never make one look
        # like ours. Unpinned, the mutex stops the release reading a STALE
        # `last_stamp` against a freshly-utimed mtime, which would make our
        # own lock read foreign -- and the wait for it is bounded, because a
        # tick can stall inside a syscall for as long as the filesystem does.
        # On expiry we LEAVE the lock rather than removing one we can no
        # longer prove is ours; the stale sweep recovers that.
        #
        # NOT `return` ON ANY ARM: this whole block is the context manager's
        # `finally`, and a `return` there discards an exception the body
        # raised.
        held_stamp = stamping is None or stamping.acquire(timeout=_RELEASE_WAIT_S)
        try:
            if not held_stamp:
                _logger.warning(
                    "Lock %s: its heartbeat did not return within %.1fs, so "
                    "the release cannot prove the lock is still ours; leaving "
                    "it for the stale sweep", lock_dir, _RELEASE_WAIT_S,
                )
            elif unproven:
                # A tick kept the lock alive without ever proving it ours, so
                # the strict comparison below would be reading a stamp that
                # tick may have taken off a successor's directory.
                _logger.warning(
                    "Lock %s: the heartbeat proceeded on an unproven stamp, "
                    "so the release cannot prove the lock is ours; leaving it "
                    "for the stale sweep", lock_dir,
                )
            elif _ours(strict=True):
                os.rmdir(lock_dir)
            else:
                # A successor's critical section would be left with nothing on
                # disk, free for a third waiter to take.
                _logger.warning(
                    "Lock %s was taken over while held; leaving it", lock_dir
                )
        except FileNotFoundError:
            _logger.warning(
                "Lock %s vanished while held (taken over as stale?)", lock_dir
            )
        except OSError as e:
            _logger.warning("Failed to release lock %s: %s", lock_dir, e)
        finally:
            if held_stamp and stamping is not None:
                stamping.release()
            # LAST, so the inode stays pinned across the identity STAT above.
            # Closing first reopens the window that stat exists to shut. It
            # does not cover stat-to-rmdir: `os.rmdir` is by NAME and removes
            # whatever is at the name then, which no descriptor can change --
            # a window inherent to a name-based directory lock and unchanged
            # from what this replaced.
            if held_fd >= 0:
                os.close(held_fd)

@contextmanager
def claude_credentials_lock(*, timeout: float | None = None):
    """Hold Claude Code's credential-refresh locks, in CC's own order.

    2.1.218 takes ``<config-home>/.oauth_refresh.lock`` first, then the
    legacy ``~/.claude.lock``; on legacy contention it releases the primary
    before retrying. Mirroring both the pair and the order means a waiting
    cswap and a waiting Claude Code can never deadlock against each other,
    and exclusion holds even after CC drops the legacy lock. Both use CC's
    60s staleness — never steal a lock a live CC may still hold.
    """
    with (
        proper_lockfile(
            oauth_refresh_lock_dir(),
            timeout=timeout,
            staleness=CREDENTIALS_STALENESS_S,
        ),
        proper_lockfile(
            credentials_lock_dir(),
            timeout=timeout,
            staleness=CREDENTIALS_STALENESS_S,
        ),
    ):
        yield


@contextmanager
def claude_config_lock(*, timeout: float | None = None):
    """Hold Claude Code's global-config write lock (``~/.claude.json.lock``)."""
    with proper_lockfile(config_lock_dir(), timeout=timeout):
        yield
