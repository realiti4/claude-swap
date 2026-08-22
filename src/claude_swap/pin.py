"""Cloud pin: keep Remote Control and Artifacts on one account.

cswap swaps the on-disk credential, so *everything* follows the swap —
including two things that are not inference and that you usually want to stay
put:

- **Remote Control** — a session's owner is fixed at creation by whichever
  bearer created it. Swap accounts and the phone/web loses the session.
- **Artifacts** — owned by the publishing bearer. After a swap a republish
  403s and the artifact "disappears" from the account you are logged into.

Claude Code resolves all of these through one credential accessor and has no
per-operation token selector, so splitting auth per operation inside a single
session means intercepting the requests. That interception lives in a separate
package (``cswap-pin``, installed via the ``pin`` extra) rather than here.

Built on ``cswap-pin`` (an optional extra). This module is import-safe without
it by design — the helpers below are pure and unit-testable in CI, and the
dependency is imported lazily inside the entry points, exactly as
:mod:`claude_swap.menubar` does with ``rumps``.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
from types import ModuleType

from claude_swap.exceptions import ClaudeSwitchError, ConfigError

_logger = logging.getLogger("claude-swap")

# -- clear_wiring stays HERE, deliberately ------------------------------------
#
# `cswap pin --clear` is priority 1 and this is the whole reason it lives in
# cswap rather than the optional package: the case it exists for is the package
# being BROKEN or GONE. A wiring left behind by a dead install points every new
# session at a port nothing serves, and only code that does not need the
# package can remove it. Delegating this would make the one path that must
# survive the package's death depend on the package.
#
# `wire_launch_env` is the opposite case and DID move: with no package there is
# nothing to wire, and returning `env` unchanged is the whole correct answer.


# -- the wiring detectors live HERE, with the remover they drive -------------
#
# `clear_wiring` is in cswap because the case it exists for is the package
# being broken or gone. These decide WHETHER it runs, so putting them in the
# package moved the guarantee out from under its own function: with the extra
# absent they answered "nothing is wired" and the stay-behind remover became
# unreachable. `serving_port`, `--get_certdir` and `--set_port` each say in
# their own comments that they work without the package; `_certdir` returning
# None made the first of those a TypeError.
#
# None of this is pin policy. It is path arithmetic on cswap's backup dir, a
# read of cswap's own config, a loopback connect, and a verdict over cswap's
# own files.


def _certdir(switcher):
    """Where the pin keeps its own files. One definition, so a layout change
    is one edit rather than a grep.

    THIS DOCSTRING HAS BEEN FALSE TWICE. It first said "all three go through
    it now" while two sites still spelled `backup_dir / "pin-proxy"`
    themselves; those were routed here, and the SAME diff then grew two more —
    `pin_is_applying` and `--get_certdir`, the command whose entire purpose is
    being the single authority on this path. A prose claim about a grep is a
    claim nothing checks, so it drifts every time somebody needs the path in a
    hurry.

    `test_the_certdir_literal_appears_exactly_once` is what makes it true now.
    Adding a third spelling fails the suite instead of aging into another
    aspirational sentence."""
    from pathlib import Path

    return Path(switcher.backup_dir) / "pin-proxy"


def _port_answers(port: int, connect_timeout: float) -> bool:
    """Does a loopback connect to ``port`` succeed? The one probe, once.

    Extracted because two callers need it and they need DIFFERENT shapes of
    the answer: :func:`_wired_port_is_serving` wants the machine-wide AND,
    :func:`_dead_wired_configs` wants it per config. Two copies of the probe
    would be two places for a timeout or an exception class to drift.
    """
    import socket

    # INSIDE THE TRY, both of them. `socket.socket()` raises `OSError` on fd
    # exhaustion (EMFILE/ENFILE) and it sat OUTSIDE — so on a starved box the
    # probe raised through `_wired_port_is_serving`, which `heal` calls twice
    # with no guard, out of the function whose contract is "never raises".
    # Nothing about "can I reach this port" should be able to end the command
    # that answers it -- and `heal`'s callers are a launch hook and a repair
    # someone is waiting on, so ending it is ending those.
    # AND `sock` IS BOUND FIRST, or the fix moves the raise instead of
    # removing it: with the construction inside the `try`, a failing
    # `socket()` leaves the name unbound and `finally` raises
    # `UnboundLocalError` — not an `OSError`, so it escapes the handler that
    # was just widened to catch it.
    sock = None
    try:
        sock = socket.socket()
        sock.settimeout(connect_timeout)
        sock.connect(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        if sock is not None:
            sock.close()
    return True


def _port_of_config(path) -> int | None:
    """The pin port ONE config file names, or None when it names none, is
    unreadable, or malformed. The single-file read :func:`_wired_ports`
    builds on, so a config's own answer is asked once.

    RANGE-CHECKED HERE, at the read, not at the probe. A value outside
    0-65535 is not a port at all — `int()` accepts it happily, but
    `socket.connect` raises `OverflowError` for it, a type
    `_wired_port_is_serving` never catches (it only catches `OSError`), and
    both its call sites inside `heal` sit OUTSIDE its bottom `try`. That
    turned a malformed `CSWAP_PIN_PORT` (any hand-edit or future writer bug,
    e.g. 99999, 70000, -1, 4294967296) into a traceback out of `cswap pin
    --heal`, out of the function whose contract is "never raises". Treating
    it as "no opinion" here, at the source, means every downstream consumer
    inherits the fix for free.
    """

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        # ONLY A PORT THIS TOOL WIRED. The marker is the receipt; a
        # ``CSWAP_PIN_PORT`` without one was put there by something else, and
        # its liveness says nothing about ours. Reading it anyway let a foreign
        # dead port make the staleness verdict True while OUR wiring was marked
        # and serving — and ``heal`` then tore down the healthy one. The
        # marker check lived only in ``_wiring_present``, one scope up, so
        # every port-level consumer inherited the gap.
        #
        # THROUGH ``_wire_mark_of``, not a fresh isinstance. That helper exists
        # because two readers of this same marker disagreed once and `--clear`
        # never converged; a third reader written here would be a fourth
        # opinion on one fact. It is the stricter test — a marker must be a
        # NON-EMPTY list — and asking it here is what makes "names a port" and
        # "is wired" the same question everywhere.
        if _wire_mark_of(raw, path) is None:
            return None
        env = raw.get("env") or {}
        port = int(env.get("CSWAP_PIN_PORT") or 0)
    except Exception:  # noqa: BLE001 — unreadable/unwired: no opinion
        return None
    return port if 0 < port <= 65535 else None


def _dead_wired_configs(_switcher, connect_timeout: float = 2.0) -> list:
    """Every wired config whose OWN port is not answering — and no more.

    THE VERDICT IS MACHINE-WIDE; THE ACT MUST NOT BE. `_wired_port_is_serving`
    is AND over every wired config on purpose, so one dead config makes the
    whole machine "not serving" — correct, because a live session config must
    not mask a dead default config. But `clear_wiring` is unconditional over
    every wired config, and composing the two unwired the OTHER config's live,
    correctly-routed pin:

        session cfg -> 42967 (LIVE)   default cfg -> 39967 (DEAD)
        stale verdict : True      ->  clear_wiring strips BOTH

    which is this file's own rule broken by its own code path — see the
    capitals at the top of :func:`heal`. The per-config answer was already
    computed by `_port_of_config`; it just was not used to decide WHICH.

    THIS LIST IS ALSO THE STALENESS VERDICT, one bool wide: "should any wiring
    be removed" is exactly "is any wired config's own port dead". A separate
    ``_wiring_is_stale`` predicate held that answer until every call site moved
    here, and it was deleted rather than kept as a one-line shim — one decision
    with two implementations is how the two drift apart, which this module's
    header warns about and which its `clear_wiring` call sites had already
    demonstrated.

    "Is any of this cswap's to condemn at all" is asked by
    :func:`_port_of_config`, once per config, and not again here — see the
    comment below for what that replaced.
    """
    # BOTH GUARDS THAT STOOD HERE ARE ENFORCED ONE SCOPE DOWN, and asking them
    # again was a leftover from before they moved. `_port_of_config` runs
    # `_wire_mark_of` itself and range-checks the port, so a config without
    # cswap's marker and a config whose port cannot be read BOTH yield None and
    # are skipped by the comprehension below — which is also what made
    # `_wired_ports()` (the same comprehension over the same reader) unable to
    # change this answer.
    #
    # THE TWO FACTS IT WAS KEEPING ARE STILL TRUE, and both are documented
    # where they are now enforced (`_port_of_config`):  A foreign
    # `CSWAP_PIN_PORT` with no marker — a future `cswap-pin` that stops writing
    # it, or an unrelated var of the same name — must not make this list non-
    # empty, or `heal` reports "Removed a cloud pin wiring…" over a byte-for-
    # byte unchanged config. Nothing is ever mutated (`_clear_wiring_locked`
    # refuses a markerless file); the damage is entirely in the VERDICT.
    #
    # "I CANNOT TELL" IS NOT "IT IS DEAD". A config carrying the marker with no
    # readable port satisfies "wired" and "not serving" at once, and the launch
    # path tore it down against a proxy that may be perfectly live. Per-config,
    # that read sees None and the config is skipped rather than cleared — which
    # is what makes the ACT per-config while the verdict stays machine-wide.
    return [
        path
        for path in _each_config()
        if (port := _port_of_config(path)) and not _port_answers(port, connect_timeout)
    ]


def clear_wiring(switcher, timeout: float | None = None, only=None) -> bool:
    """Remove a pin wiring from the global config. True when it removed one.

    ``only`` narrows it to the given config paths. The default — every wired
    config — is what ``cswap pin --clear`` means and must not change: the user
    asked to be unpinned, and leaving one config wired is the stranding this
    function exists to prevent. ``heal`` is the caller that needs less than
    that, because its trigger is per-config (see :func:`_dead_wired_configs`)
    while its remedy was not.

    The pin writes its proxy address into ``.claude.json``'s env block and
    records which keys it wrote in ``_cswapPinWiredKeys``; this reads that
    marker and puts the file back. It touches no proxy, no daemon and no
    credential — only a record cswap left.

    It has to be here rather than in the optional package because the failure
    it prevents is caused by that package being GONE. Claude Code applies the
    env block at boot, so a wiring naming a port nothing listens on makes
    every hand-launched ``claude`` dial a dead proxy and retry forever. If the
    only code able to remove it shipped in the pin package, uninstalling the
    pin — the very thing an optional extra invites — would strand the wiring
    permanently, with hand-editing ``.claude.json`` the sole cure.

    Only keys the pin recorded are touched, and anything it displaced is
    restored, so a proxy the user or their launcher set beforehand comes back
    rather than being lost with ours.
    """
    proper_lockfile = __import__("claude_swap.claude_locks", fromlist=["x"]).proper_lockfile
    get_default_global_config_path = __import__("claude_swap.paths", fromlist=["x"]).get_default_global_config_path
    get_global_config_path = __import__("claude_swap.paths", fromlist=["x"]).get_global_config_path

    # BOTH configs, because the writing side resolves the same way this does:
    # `CLAUDE_CONFIG_DIR` is set in the *child's* env dict, not the process's,
    # so a `cswap run` from a normal terminal wires ~/.claude.json while one
    # from inside a session terminal wires that session's copy. Clearing only
    # the resolved path leaves the other wired, and `cswap pin --clear` then
    # prints "No cloud account pinned" over a config that still names a dead
    # port — the exact stranding this function exists to prevent. The two paths
    # diverge as soon as CLAUDE_CONFIG_DIR is set.
    #
    # EACH GETTER CAN RAISE (see the same guard on `_wired_ports` and
    # `_wiring_present`): `get_default_global_config_path` calls `Path.home()`,
    # which raises `RuntimeError` with no HOME and no `/etc/passwd` entry. A
    # config this call cannot even locate has nothing to clear there — that is
    # a fact about ONE config, not a reason to abandon the other. LOGGED, not
    # just skipped: a config that could not be RESOLVED and one that resolved
    # with nothing wired both leave this loop silently short a path, and
    # `clear_wiring`'s bool is a claim about every path it reached — not a
    # claim that every path was reachable. Without a record, "the default
    # profile was never attempted because HOME could not be found" and "the
    # default profile was attempted and had nothing wired" are the same silence
    # from the outside.
    #
    # WARNING HERE ONLY, which is the whole reason this passes a level. `heal`
    # reaches `clear_wiring` through `_dead_wired_configs`, which goes empty
    # ONCE THE REMOVAL SUCCEEDS, so this logs once and goes quiet. The two
    # getters `heal` calls UNCONDITIONALLY stay at DEBUG (see
    # `_log_unresolvable`).
    #
    # THIS RECORD DOES NOT EXPLAIN AN UNREMOVABLE WIRING, and must not be read
    # as if it did. On the flagship shape — read-only config dir, HOME
    # resolvable — nothing raises here and it never fires; what fires is
    # `heal`'s own "the config is locked" message. Make `Path.home()` raise too
    # and this names `get_default_global_config_path` while the STUCK config is
    # the one the other getter resolved fine. Put the wiring in the raising
    # getter's config and `_wiring_present` cannot see it either, so `heal`
    # answers "Nothing to heal" and never reaches this function. What names an
    # unremovable wiring is the lock-failure WARNING at the bottom of this
    # function. This record's job is the narrower one it can do: a config that
    # could not be LOCATED is missing from `paths`, and `clear_wiring`'s bool
    # is a claim about every path it REACHED.
    paths = list(_each_config(logging.WARNING))
    if only is not None:
        # BY RESOLVED PATH, not by identity: the caller got its list from
        # `_each_config` too, but a getter that resolves through a symlink or
        # a different Path flavour would silently filter everything out — and
        # an empty `paths` here is a clear that removes nothing while
        # reporting the same False as "there was nothing to remove".
        wanted = {str(p) for p in only}
        paths = [p for p in paths if str(p) in wanted]

    # ONE LOCK PER PATH. The shared config lock derives its directory from
    # get_global_config_path(), so a single lock around the loop guards one
    # file and leaves the other rewritten unprotected — racing `cswap switch`
    # and Claude Code, the whole-file clobber the lock exists to prevent.
    #
    # ``timeout`` is a TOTAL, not a per-file allowance. Passing it to each
    # acquisition makes the worst case a multiple of the number of configs, so
    # the launch path's sub-second cap silently becomes ~2x that (1.37-1.64s
    # against a documented 0.5s).
    import time as _time

    # An UNTIMED call still gets a total. Leaving `None` lets each config
    # independently wait the lock's own default, so `cswap pin --clear` with
    # both locks held freezes for 2x that (18.18s against a 9s default) — the
    # same multiple-of-the-configs shape, on the branch with no timeout.
    if timeout is None:
        DEFAULT_TIMEOUT_S = __import__("claude_swap.claude_locks", fromlist=["x"]).DEFAULT_TIMEOUT_S

        timeout = DEFAULT_TIMEOUT_S
    deadline = _time.monotonic() + timeout
    changed = False
    for i, path in enumerate(paths):
        # EVERY PATH IS ATTEMPTED, even with the budget gone. `if left <= 0:
        # continue` was here, and it is the same starvation the fair share
        # below was introduced to fix, one runner-speed away: path 1 only has
        # to OVERSHOOT its share for path 2 to be skipped without a single
        # attempt. A zero share is not a skip: `proper_lockfile` tries
        # `os.mkdir` BEFORE it checks its deadline, so a FREE lock is taken
        # instantly and a contended one fails at once. The cost of the change
        # is a few syscalls past the budget; the cost of the skip was `cswap
        # pin --clear` returning with the second config still wired.
        left = max(0.0, deadline - _time.monotonic())
        # FAIR SHARE of what remains, not "however much is left". Handing the
        # first path the whole remaining budget let a config that stayed
        # contended for the entire call consume it all, so a SECOND path
        # whose lock was completely free was skipped by the `left <= 0` check
        # above without ever being tried: with the session lock held for the
        # full 0.5s budget, `clear_wiring` returns False with BOTH configs
        # still wired.
        #
        # Dividing by how many paths are still untried gives each one at
        # least an equal slice of whatever time remains when its turn comes,
        # while the running total can still never exceed `timeout` — each
        # share is carved out of `left`, never added to it.
        share = left / (len(paths) - i)
        try:
            with proper_lockfile(
                path.parent / (path.name + ".lock"), timeout=share
            ):
                if _clear_wiring_locked(switcher, path):
                    changed = True
        except Exception as exc:  # noqa: BLE001
            # A lock we cannot take is a reason to skip THIS file, not to
            # abandon the other one — and on the launch path (sub-second
            # budget) a contended config must not fail the clear outright.
            #
            # BUT SAY WHICH FILE AND WHY. This is the ONLY record naming which
            # config could not be unwired and what stopped it. Skipping
            # silently leaves the flagship failure — a read-only config dir,
            # HOME resolvable — telling the user "could not be removed (the
            # config is locked)" every tick with zero records at any level. The
            # getter WARNING above does not fire on that shape.
            #
            # KEPT AT WARNING FOR BOTH REACHABLE KINDS. `PermissionError` and
            # `ClaudeCodeLockTimeout` both land here, and the type does not
            # separate transient from permanent: a live Claude Code credential
            # refresh raises the timeout, and so does an orphaned lock dir
            # inside a directory this process cannot write, which never
            # resolves. Splitting on type would silence the stuck machine this
            # WARNING exists for. The lock dir's mtime age WOULD separate them
            # (`proper_lockfile` already reads it against
            # `CONFIG_STALENESS_S`), but the transient case is self-limiting —
            # the competitor lets go and the next free tick unwires — so it
            # costs ~2 lines once, against a permanent case that repeats
            # forever. Not worth the arithmetic.
            _logger.warning("%s could not be unwired: %s", path, exc)
            continue
    return changed


def _clear_wiring_locked(switcher, path) -> bool:
    """The read-modify-write of :func:`clear_wiring`, under its lock."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict):
        return False

    ours = _wire_mark_of(raw, path)
    if ours is None:
        return False  # nothing of ours in there

    env = raw.get("env")
    env = dict(env) if isinstance(env, dict) else {}
    saved = _saved_of(raw, path)
    for key in ours:
        env.pop(key, None)
    env.update(saved)

    # BOTH LOCATIONS, always. The receipt may live in either (see
    # `_wire_mark_of`), and clearing only the one we read from leaves the other
    # claiming a wiring whose proxy vars are already gone — which every
    # "is it wired" caller then believes.
    raw.pop(_WIRE_MARK, None)
    raw.pop(f"{_WIRE_MARK}Saved", None)
    if env:
        raw["env"] = env
    else:
        raw.pop("env", None)

    try:
        # The switcher's own writer, not a second one: it already validates the
        # JSON it produced and chmods the TEMP file so the rename is the atomic
        # commit. This file can hold ``primaryApiKey`` and inline MCP
        # credentials, so a hand-rolled write here would be a second place for
        # that 0600 to drift out of agreement with switcher.py.
        switcher._write_json(path, raw)
    except (OSError, ConfigError):
        return False
    # AFTER the config write, never before. This is the receipt for what the
    # config still holds; dropping it first and then failing to write the
    # config would leave the proxy vars in place with nothing recording that
    # they are ours — unremovable except by hand, the exact failure
    # `clear_wiring` exists to prevent.
    #
    # GATED ON THE SIDECAR, not only the config. Both receipts have to go, or
    # `_wiring_present` reads the survivor and the caller is told to re-run a
    # command that cannot converge.
    return _clear_ledger(path)


def _each_config(level: int = logging.DEBUG):
    """Both global configs, in read order, de-duplicated, guards applied.

    THE GETTER ITSELF CAN RAISE, and that is why this exists as one function
    rather than three loops. ``get_default_global_config_path`` calls
    ``Path.home()``, which raises ``RuntimeError`` when HOME is unset and the
    uid has no ``/etc/passwd`` entry (the standard rootless-container shape).
    ``heal``'s contract is "never raises" because a launch hook runs it before
    every hand-launched ``claude``, and ``_wired_ports`` sits on the path from
    ``heal`` through ``_wired_port_is_serving`` with no guard above it — an
    unguarded raise there leaves ``pin.heal(sw)`` raising ``RuntimeError`` at
    the launch instead of returning ``(False, 'Could not heal…')``.

    A config this cannot even LOCATE has no opinion — a fact about ONE config,
    never a reason to abandon the other, which is why it continues rather than
    propagating.

    ``level`` is the caller's, and only ``clear_wiring`` raises it to WARNING;
    see the comment at that call site for why that one is allowed to be loud
    and the two ``heal`` calls unconditionally are not.

    De-duplicated because the two getters return the SAME path whenever
    ``CLAUDE_CONFIG_DIR`` is unset, and every caller would otherwise do its
    work on that config twice.
    """
    get_default_global_config_path = __import__("claude_swap.paths", fromlist=["x"]).get_default_global_config_path
    get_global_config_path = __import__("claude_swap.paths", fromlist=["x"]).get_global_config_path

    seen = set()
    for get in (get_global_config_path, get_default_global_config_path):
        try:
            path = get()
        except Exception as exc:  # noqa: BLE001 — unresolvable: no opinion
            _log_unresolvable(get, exc, level)
            continue
        if path in seen:
            continue
        seen.add(path)
        yield path


def _clear_ledger(config_path) -> bool:
    """Record "not wired" in the sidecar. Never raises; SAYS whether it wrote.

    THE RETURN IS LOAD-BEARING, and discarding it made `--clear` permanently
    non-converging. The config write could succeed while this one failed (an
    unwritable pin-wiring dir, a full disk, a root-owned parent) and
    `_clear_wiring_locked` still returned True. The sidecar then kept a
    non-empty marker over a config that was already clean, so `_wiring_present`
    stayed true forever: every re-run re-injected the recorded values, failed
    the same way, and answered "re-run once it frees up" — advice that could
    never come true. The TUI kept showing a phantom cloud-account row on top.

    WRITES AN EMPTY MARKER rather than deleting the file. `_wire_mark_of`
    treats a sidecar that says "not wired" as the answer FOR THE SIDECAR and
    stops there when the config carries no marker of its own; a DELETED
    sidecar is a miss, so unlinking would let an old config key that a failed
    earlier write left behind resurrect a wiring this call just removed.

    It does NOT silence a marker in the config: that is a receipt this clear
    never saw, and reading the empty sidecar as an answer for BOTH locations
    made an older cswap-pin's wiring invisible to every recovery path.
    """
    path = None
    try:
        path = _ledger_path(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({_WIRE_MARK: []}), encoding="utf-8")
        # 0600 BEFORE the rename, like every other writer in this store. The
        # ambient umask put this file at 0644 next to the package's 0600 ones
        # in the same directory. The contents are key NAMES, not secrets, so
        # this is consistency rather than exposure — but a store where the
        # mode depends on which component wrote last is one someone will
        # eventually read the wrong way.
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001 — the config write is what matters
        if path is not None:
            try:
                path.with_name(f"{path.name}.{os.getpid()}.tmp").unlink()
            except OSError:
                pass
        return False


def _saved_of(raw: object, config_path=None) -> dict:
    """What the wiring displaced, from wherever the receipt lives.

    Same read-both rule as :func:`_wire_mark_of`, and it must stay paired with
    it: reading the marker from the sidecar and the displaced values from the
    config would restore one wiring's values over another's keys.
    """
    if config_path is not None:
        side = _read_ledger(config_path)
        if _WIRE_MARK in side:
            # PAIRED WITH THE MARKER, not merely with the sidecar's existence.
            # `_wire_mark_of` falls through to the config when the sidecar is
            # EMPTY and the config carries a marker of its own; reading the
            # displaced values from the sidecar in that case would restore one
            # wiring's values over another wiring's keys — which is worse than
            # restoring nothing, because it writes a proxy address that was
            # never there.
            side_mark = side.get(_WIRE_MARK)
            if isinstance(side_mark, list) and side_mark:
                saved = side.get(f"{_WIRE_MARK}Saved")
                return dict(saved) if isinstance(saved, dict) else {}
            if not (
                isinstance(raw, dict)
                and isinstance(raw.get(_WIRE_MARK), list)
                and raw.get(_WIRE_MARK)
            ):
                saved = side.get(f"{_WIRE_MARK}Saved")
                return dict(saved) if isinstance(saved, dict) else {}
    if not isinstance(raw, dict):
        return {}
    saved = raw.get(f"{_WIRE_MARK}Saved")
    return dict(saved) if isinstance(saved, dict) else {}


def _wire_mark_of(raw: object, config_path=None) -> list | None:
    """The marker THIS module wrote, or None. The single reader.

    ``_wiring_present`` and ``_clear_wiring_locked`` both answer "is it
    wired", and they disagreed: one accepted any truthy marker, the other
    required a non-empty list. A malformed marker (a hand-edit, a format
    change in a future cswap-pin) therefore satisfied the first and not the
    second, so `--clear` reported "could not remove the wiring — re-run once
    it frees up" forever: nothing was contended and nothing ever converged.

    That single-reader property is what makes the sidecar safe to add: the
    "read both locations" rule is written HERE, once, so every caller gets it
    without knowing the receipt moved.
    """
    if config_path is not None:
        side = _read_ledger(config_path)
        ours = side.get(_WIRE_MARK)
        if isinstance(ours, list) and ours:
            return ours
        # AN EMPTY SIDECAR ANSWERS FOR THE SIDECAR, NOT FOR THE CONFIG.
        #
        # `--clear` empties it, and treating that as the final answer made
        # a wiring written by an OLDER cswap-pin — which writes the config
        # key and no sidecar, the compat promise stated above — invisible
        # to every recovery path at once:
        #
        #     _wiring_present  False    _wired_ports  []
        #     _dead_wired_configs []     clear_wiring  False
        #     heal             (False, 'Nothing to heal')
        #
        # while `.claude.json` still named a proxy port. Every probe that
        # could have caught the stranding reported healthy. The population
        # is not exotic: the extra carries no floor, so an already-present
        # `cswap-pin` is never upgraded, and anyone who installed the pin
        # before the sidecar existed lands here on their first clear.
        #
        # What the empty sidecar DOES rule out is resurrecting a receipt
        # the clear emptied — but only the one it emptied. A marker in the
        # config is a receipt the clear never saw.
        if _WIRE_MARK in side and not (
            isinstance(raw, dict)
            and isinstance(raw.get(_WIRE_MARK), list)
            and raw.get(_WIRE_MARK)
        ):
            return None
    if not isinstance(raw, dict):
        return None
    ours = raw.get(_WIRE_MARK)
    return ours if isinstance(ours, list) and ours else None


def _ledger_path(config_path):
    """The sidecar receipt for ``config_path``, under the account store.

    KEYED BY CONFIG PATH, because there are two configs. `cswap run` from a
    normal terminal wires `~/.claude.json`; from inside a session terminal it
    wires that session's `CLAUDE_CONFIG_DIR` copy. One sidecar for both would
    have the second wiring's receipt overwrite the first's, and unwiring would
    then restore the wrong displaced values into the wrong file — worse than
    the config key it replaces, which at least travelled WITH its config.
    """
    import hashlib

    get_backup_root = __import__("claude_swap.paths", fromlist=["x"]).get_backup_root

    key = hashlib.sha256(str(config_path).encode("utf-8")).hexdigest()[:16]
    return get_backup_root() / "pin-wiring" / f"{key}.json"


def _read_ledger(config_path) -> dict:
    """The sidecar receipt, or an empty one when there is none to read.

    ``{}`` rather than ``None``: ABSENT and UNREADABLE answer every question a
    caller asks the same way an empty dict does — ``_WIRE_MARK in {}`` is
    False and ``{}.get()`` is None — so both readers were re-testing for a
    distinction neither of them made. Verified equivalent across all 56
    sidecar/config pairs before the change.

    What still differs, and must, is ``{_WIRE_MARK: []}``: that is `--clear`'s
    receipt, it answers FOR THE SIDECAR, and it carries what that clear
    displaced. Absence carries nothing.
    """
    # RESOLVING THE PATH IS ITSELF A RAISING CALL — `_ledger_path` goes through
    # `get_backup_root()`, which raises with no HOME. A receipt whose PATH
    # cannot be resolved is a machine with no backup root, i.e. no pin —
    # genuinely absent, so it answers like absent, at debug rather than in
    # silence.
    try:
        path = _ledger_path(config_path)
    except Exception as exc:  # noqa: BLE001 — no backup root means no pin
        _logger.debug("no pin receipt path for %s: %r", config_path, exc)
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # THE ORDINARY STATE. Every machine that was never pinned lands here,
        # so it must stay silent or the warning below is noise on every launch
        # and the next reader deletes it.
        return {}
    except Exception as exc:  # noqa: BLE001 — see below
        # PRESENT AND UNREADABLE IS NOT ABSENT, even though both answer the two
        # readers with `{}` — which is true, verified across all 56
        # sidecar/config pairs, and only half the story.
        #
        # Current cswap-pin writes the receipt ONLY here; the config carries no
        # marker. So a root-owned parent, a read-only mount or a truncated file
        # makes a LIVE wiring invisible to every recovery path at once:
        # `_wiring_present` False, `heal` -> "Nothing to heal", `--ensure` a
        # no-op, and `purge` printing "Removed: Cloud pin wiring" — while
        # `.claude.json` still names a dead HTTPS_PROXY that every hand-launched
        # `claude` dials.
        #
        # The RETURN stays `{}` so that equivalence is untouched. What changes
        # is that the operator hears about it, and it is said HERE because this
        # is the single read point every caller already goes through — the
        # property `_wire_mark_of`'s docstring claims for the read-both rule.
        _logger.warning(
            "%s exists but could not be read (%s), so it is treated as no pin "
            "receipt. If a pin IS wired, heal/purge/--ensure will all report "
            "nothing to do while the env block still names its proxy.",
            path, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _config_lock_is_free(budget: float) -> bool:
    """Can the config lock be taken within ``budget`` seconds?

    A probe, not a hold — the caller re-locks immediately after. That race is
    deliberate: losing it costs one skipped unwire (the next launch heals it),
    while the alternative is the launch itself waiting on the package's own
    5-second lock timeout, which it has no way to shorten.

    BOTH CONFIGS, because the operation this gates acts on both. It probed
    `get_global_config_path()` alone, and `clear_wiring` says why that is not
    the same question: with `CLAUDE_CONFIG_DIR` set the two paths diverge, so
    a free session config and a HELD `~/.claude.json` — a Claude Code
    credential refresh, say — passed the probe. `unwire_if_dead` then blocked
    on the package's own `claude_config_lock(timeout=5)`: a 5.3 s launch
    stall, ten times the `_LAUNCH_LOCK_BUDGET_S` this guard exists to enforce,
    reached THROUGH the guard.

    The budget is per config rather than shared. Two configs is the maximum,
    they are the same path whenever `CLAUDE_CONFIG_DIR` is unset (so the
    common case pays once), and splitting a sub-second budget in half makes
    each probe more likely to lose a race it would otherwise have won.
    """
    proper_lockfile = __import__("claude_swap.claude_locks", fromlist=["x"]).proper_lockfile

    for path in _each_config():
        try:
            with proper_lockfile(
                    path.parent / (path.name + ".lock"), timeout=budget):
                continue
        except Exception:  # noqa: BLE001
            return False
    return True


def _log_unresolvable(get, exc: BaseException, level: int = logging.DEBUG) -> None:
    """Record a path getter's raise, every time it happens. DEBUG by default.

    THE LEVEL IS THE CALLER'S, and only `clear_wiring` passes WARNING. No cap.

    A once-per-PROCESS cap cannot suppress anything here: `heal` runs as a
    fresh short-lived process per tick, so the cap's lifetime IS one tick.

    DEBUG is the default because `heal` calls `_wiring_present` and
    `_wired_ports` on EVERY tick regardless of wiring state; warning from
    there costs ~4.2MB/day and overwrites the whole 4MB rotating history in
    under a day. DEBUG keeps the record without paying for it every tick.

    `clear_wiring` overrides to WARNING because it is gated by
    `_dead_wired_configs`, and because a config that could not be LOCATED is the
    one fact its return value cannot carry: the bool is a claim about every
    path it REACHED. Why a wiring could not be REMOVED is a different record —
    the lock WARNING at the bottom of `clear_wiring`.
    """
    # `stacklevel=3` ATTRIBUTES THE RECORD TO THE CONSUMER. Without it all
    # three call sites' records are identical in origin — same `funcName`,
    # same `pathname`, same `lineno` — so nothing downstream can tell the
    # per-tick getters from the gated one, and a guard on this split can only
    # key on LEVEL. With it, `record.funcName` is `_wiring_present` /
    # `_wired_ports` / `clear_wiring`.
    #
    # THREE, NOT TWO, because the traversal is now one shared generator
    # (`_each_config`) rather than three copies of the loop: 2 would name
    # `_each_config` for all of them, which is precisely the collapse this
    # argument exists to prevent. Verified against a generator AND a
    # comprehension consumer — a comprehension reports its ENCLOSING function
    # (inlined since 3.12; this project's floor), not a `<listcomp>` frame.
    #
    # Production output is UNCHANGED: `logging_config` formats
    # "%(asctime)s - %(levelname)s - %(message)s" and never renders funcName,
    # filename or lineno.
    _logger.log(level, "%s could not be resolved: %s", get.__name__, exc, stacklevel=3)


# THE ABSENCE MESSAGE STAYS HERE. It tells a user how to install the package,
# so it is the one thing that must read correctly when the package is missing —
# the same contract as `clear_wiring` and `wire_launch_env`. I moved it with
# the wiring closure and two guards caught it by name.


def _install_hint() -> str:
    """How to install the extra, in a form that reaches THIS install.

    Not a constant, because `pip install` is wrong for the install method most
    users have. Under a uv tool install, pip puts a second copy in whatever pip
    is on PATH and the extra never reaches the tool's environment — the user
    follows the instruction, it succeeds, and the pin is still missing.
    `cswap upgrade` already solves this; reuse its detector rather than
    re-deriving it.

    THE ONE PLACE THAT DECIDES THE COMMAND, which is why the mapping is inline
    rather than in a helper of its own: a second hardcoded `uv tool install`
    once survived beside the derived hint and diverged from it on a pipx
    machine — one screen apart, both wrong for someone.
    `test_one_place_decides_the_install_command` enforces that by name.
    """
    _detect_install_method = __import__("claude_swap.update_check", fromlist=["x"])._detect_install_method

    how = {
        "uv": "uv tool install 'claude-swap[pin]'",
        "pipx": "pipx install 'claude-swap[pin]'",
    }.get(_detect_install_method() or "", "pip install 'claude-swap[pin]'")
    return f"The cloud pin requires 'cswap-pin'. Install with: {how}"


def wire_launch_env(switcher, env: dict[str, str]) -> dict[str, str]:
    """Route a child Claude Code through the pin proxy, if one is pinned.

    Returns ``env`` unchanged when there is no pin, when the extra is not
    installed, or when the proxy cannot be started: an optional feature must
    never be able to block a launch.
    """
    # ONE guard around everything, including _impl(). A split try leaves the
    # resolution step uncovered, so anything raised there — a broken
    # cryptography, a corrupt install — kills the launch instead of starting
    # it unpinned.
    try:
        pin = _impl()
    except Exception:  # noqa: BLE001 — never block the launch
        # No pin this launch, whatever the reason: not installed, or installed
        # and broken. A wiring a previous install left behind would otherwise
        # outlive it and point every session at a dead port — see clear_wiring.
        # ASK FIRST, LOCK ONLY IF THERE IS WORK. The budget is per PATH and
        # clear_wiring takes one lock per config, so a user who never installed
        # the pin — the case this budget exists for — would pay it twice
        # (1.37-1.64s with Claude Code holding the lock, against a 0.5s cap).
        # `_wiring_present` is lock-free, answers in ~1.5ms, and for that user
        # the answer is always "nothing to remove".
        #
        # AND NOT SERVING. `_impl()` raising says nothing about the daemon: a
        # broken cryptography, a half-finished reinstall, an import error in a
        # new release all land here while the proxy on the port keeps answering
        # every session already wired to it. Unwiring on presence alone strips
        # the env block from a healthy pin. The probe is bounded well under the
        # launch budget rather than given the default 2s: a black-holed port
        # must not turn a launch-path guard into the stall it was written to
        # avoid. `clear_wiring` logs at most twice per LAUNCH here (its getter
        # WARNING and its lock WARNING), because the gate goes false only when
        # the removal succeeds. At human launch cadence that is negligible,
        # which is why the churn arithmetic lives at the statusline call site.
        #
        # THE DEAD CONFIGS, NOT "THE WIRING" — the correction `heal` carries,
        # on the third of its three call sites. All three ask a MACHINE-WIDE
        # verdict, and answering it with a machine-wide ACT strips a live
        # session config wired to a serving port because the OTHER config names
        # a dead one. `_dead_wired_configs` keeps the verdict identical (the
        # list IS the staleness verdict, one bool wide) and narrows only what
        # gets removed.
        try:
            dead = _dead_wired_configs(switcher, connect_timeout=_LAUNCH_PROBE_S)
            if dead:
                # THE HOST'S CLEAR, NOT OURS. `clear_wiring` lives in cswap on
                # purpose — the case it exists for is this package being gone —
                # so calling a copy here would enforce that guarantee with the
                # implementation that does not survive our death.
                clear_wiring(switcher, timeout=_LAUNCH_LOCK_BUDGET_S, only=dead)
        except Exception:  # noqa: BLE001
            pass
        return env
    try:
        pinned = pin.ensure_proxy(switcher)
        if pinned:
            port, ca_path = pinned
            # A COPY, so the peer can only scribble on a throwaway. The
            # validation below covers what `wire_env` RETURNS; a version that
            # also WRITES would leave a half-wired env in the object the
            # caller keeps — `session.py` passes the dict it goes on to use —
            # and in the one this function falls back to returning, which
            # reaches `os.execvpe` OUTSIDE the launch's try. There a wrong
            # shape is not a caught exception, it is the launch.
            #
            # Today's 0.1.68 opens with `out = dict(env)` and does not write.
            # But this module's stated threat model is a peer on an
            # independent release schedule: `heal` already refuses to trust
            # its return value, and trusting it not to WRITE while validating
            # what it returns was the missing half. One `dict()` here covers
            # the caller's object and the fallback path together.
            wired = pin.wire_env(dict(env), port, ca_path)
            # VALIDATED, NOT TRUSTED. This value reaches `os.execvpe`, which
            # sits OUTSIDE the launch's try, so a wrong shape is the launch
            # rather than a caught exception. `None` does not even fail: it
            # hands the child the PARENT's environ, dropping
            # CLAUDE_CONFIG_DIR, so the session runs against the default
            # login. That silent one is why this is a check and not a try.
            # Anything that is not a str->str mapping degrades to an UNPINNED
            # launch, which this file is built to tolerate.
            if isinstance(wired, dict) and all(
                isinstance(k, str) and isinstance(v, str) for k, v in wired.items()
            ):
                return wired
    except Exception:  # noqa: BLE001 — never block the launch
        pass
    # No proxy this launch, whether ensure_proxy said so or died saying it.
    # .claude.json's env block is applied at boot, so a wiring a previous
    # launch left behind would send this child at a port nothing answers.
    # ONE tail, not one per branch: duplicating it runs the unwire twice when
    # the None path's own unwire raises.
    #
    # BOUNDED, like the no-package branch above. `unwire_if_dead` takes no
    # timeout and uses the package's own claude_config_lock(timeout=5), so a
    # held .claude.json.lock costs every `cswap run` 5.3s before it returns the
    # env unchanged — and Claude Code holds that lock routinely while
    # refreshing credentials.
    #
    # If the lock is not free right now, SKIP: the wiring is stale but the next
    # launch heals it, and a launch that blocks is worse than a launch that is
    # briefly unpinned — the whole reason this path fails open.
    try:
        if _config_lock_is_free(_LAUNCH_LOCK_BUDGET_S):
            pin.unwire_if_dead(_certdir(switcher))
    except Exception:  # noqa: BLE001
        pass
    return env


def _impl() -> ModuleType:
    """The pin implementation, or a clean error naming the fix.

    Raises the type the CLI already renders rather than letting an
    ``ImportError`` traceback out of a command the user typed.

    "Not installed" and "installed but broken" are separated by ``find_spec``
    instead of by catching ``ImportError``. Catching cannot tell them apart,
    and conflating them tells the user to install a package they already have
    when the real cause is, say, a missing ``cryptography``. A failure raised
    from inside a module that IS present propagates unchanged.
    """
    import importlib.util
    import sys

    # POSIX only, the same way the menu bar is macOS only. The proxy holds its
    # daemon lock with fcntl.flock and refcounts sessions through a FIFO
    # (os.mkfifo); neither exists on Windows, so an install there would fail at
    # first use with a ModuleNotFoundError from inside the dependency rather
    # than a sentence the user can act on. cswap itself advertises Windows
    # support (pyproject classifiers), so this has to be said, not assumed.
    if sys.platform == "win32":
        raise ClaudeSwitchError(
            "The cloud pin is not available on Windows: it needs POSIX file "
            "locks and FIFOs."
        )

    try:
        found = importlib.util.find_spec("cswap_pin.proxy") is not None
    except ImportError as exc:
        # find_spec has to IMPORT the parent package to read its __path__, so a
        # cswap_pin/__init__.py that raises surfaces here rather than below —
        # and swallowing it is what turns "your cryptography is broken" into
        # "install the package you already have". A package root raising
        # ImportError("No module named 'cryptography'") propagates out of
        # find_spec, not out of import_module.
        #
        # e.name is what tells them apart: absent -> 'cswap_pin', broken root
        # -> whatever the package failed to import.
        if exc.name and not exc.name.startswith("cswap_pin"):
            raise
        found = False
    except ValueError:
        found = False
    if not found:
        raise ClaudeSwitchError(_install_hint())
    # NO RUNTIME VERSION FLOOR. The extra's floor lives in ONE place, the
    # `pin = ["cswap-pin>=X"]` requirement, exactly as the menubar extra
    # declares `rumps>=0.4.0` and then only asks whether the import works.
    #
    # A hardcoded tuple here was the alternative, and it does not survive
    # contact with the release cycle: cswap-pin ships on its own schedule, so
    # every release of it would need a matching pull request against THIS
    # project just to raise a constant. A gate whose maintenance depends on
    # someone else's release cadence is a gate that goes stale, and a stale
    # floor is worse than none — it refuses a package the installer has just
    # chosen, with a message blaming the user's version.
    #
    # Keeping a released version out is an INSTALL-time job (the requirement,
    # and whatever provisioning runs it), not something the seam re-litigates
    # on every call.
    return importlib.import_module("cswap_pin.proxy")


def _live_impl() -> ModuleType | None:
    """The implementation if it is usable RIGHT NOW, else None. Never raises.

    Both display helpers below need the same thing: resolve the package, and
    treat every failure as "no pin" rather than an error. Callers that ACT on
    the pin use :func:`_impl` instead and report what it raises — hiding a
    broken install is right for a badge and wrong for a command.

    ``invalidate_caches`` because a long-lived process caches each sys.path
    directory by mtime, so an install landing inside the same mtime tick stays
    invisible without it — the "I installed it and the menu is still missing"
    case.

    NOT MEMOISED, deliberately. A 1.0s TTL lived here, with a module-level
    cache, a ``global``, and an autouse conftest fixture to reset it between
    tests. Measured against what it bought: 185us per uncached call, and a
    render tick makes 6 calls at 3 accounts / 13 at 10 — 1.11ms and 2.40ms
    against POLL_INTERVAL_S of 3000ms, so 0.04-0.08% of one tick. It also
    made the thing it was supposed to protect worse: an install landing
    INSIDE the TTL window was invisible until the window expired, and the
    test that "proved" otherwise advanced a fake clock past the TTL, so it
    asserted the cache's own contract rather than the user's.

    THE CONFIG READS AROUND IT WERE RE-RAISED AS A STALL AND RE-MEASURED
    2026-08-18. `_root_entries` runs on the 3s watcher and reaches
    `_wiring_present` and `_pinned_email_now`, each of which parses the global
    config. On this fleet's real file: 175 KB, 1.1 ms per read+parse, so the
    whole render tick spends ~5 ms of 3000 — 0.2%. The "megabytes on a real
    machine" that motivates `_dead_wired_configs`' own frugality is a
    different machine's file, and none on this fleet is one. Cache it when a
    measurement shows a tick that matters, not because the shape looks
    expensive; the last cache here cost more than it saved.
    """
    import importlib

    importlib.invalidate_caches()
    try:
        return _impl()
    except Exception:  # noqa: BLE001
        return None


def is_available() -> bool:
    """Whether a pin surface should be shown at all."""
    return _live_impl() is not None


def pinned_email(switcher) -> str | None:
    """The pinned account's email, or None.

    The TUI's one question about the pin is "which account is it on", and
    None is the honest answer in every failure: no extra, no pin, a malformed
    pin file. With no extra there IS no pin, so a notice would be a permanent
    banner on machines that deliberately run without it, and an always-on
    warning is one people stop reading.
    """
    impl = _live_impl()
    if impl is None:
        return None
    try:
        pin = impl.load_pin(switcher.backup_dir)
    except Exception:  # noqa: BLE001 — a badge must not take the view down
        return None
    return pin[0] if pin else None


def pinned_identity(switcher) -> "tuple[str, str] | None":
    """The pinned account as `(email, organizationUuid)`, or None.

    THE COMPOSITE, because an email does not identify an account here: two
    managed slots may share one across organizations, and a person may hold a
    personal account at the same address as an org one. `_pinned_email_now`
    already returns both and `pinned_identity_email` discarded the second, so
    the only reader that could tell a splice from a genuine login as a
    same-email sibling was comparing on the half that does not decide.

    Never raises, for the reason `pinned_identity_email` documents.
    """
    try:
        return _pinned_email_now(switcher)
    except Exception:  # noqa: BLE001 — an optional extra cannot break a read
        return None


def _live_login_for_config(switcher) -> "dict | None":
    """The identity the config should carry when NO pin is being named.

    Both callers reach this by the same route -- a clear, and a rollback with
    nothing to restore -- and both mean "hand the machine back to whoever is
    actually logged in". `_live_login_identity` un-splices, which is the
    point: under a pin the config names the pin, and the account that has been
    SERVING is the one the config should name once the pin is gone.

    None on any doubt, which the package reads as "leave the field alone".
    """
    live = switcher._live_login_identity()
    if not live:
        return None
    return identity_for_config(switcher, email=live[0],
                               num=switcher.current_account_number())


def pinned_slot(switcher) -> "str | None":
    """The roster slot the pin names, or None when it cannot be told.

    THE SEAM cswap core asks so it does not have to re-derive this. The
    autoswitch tick wants to keep the rotation off the pinned account — the
    pin's own window is what Remote Control spends, and ordinary inference on
    the same slot drains it. That is a question about the pin, so it is
    answered here rather than from proxy.json, which would drift.

    ON THE COMPOSITE, NOT THE ADDRESS: two managed slots can share one email
    across organizations, and this roster already has such a pair. Keyed on
    the address alone the answer is whichever comes first, silently wrong for
    the other.

    NEVER RAISES, because it runs on a 15-60 second tick beside the switch.
    None is the honest answer to "cannot tell", and the caller then behaves
    exactly as it does today.
    """
    try:
        ident = _pinned_email_now(switcher)
    except Exception:  # noqa: BLE001 — a tick must not die on a tidy question
        return None
    if not ident:
        return None
    email, org = ident
    return _slot_for(switcher, email, org)


def _slot_for(switcher, email: "str | None", org_uuid: "str | None"):
    """The roster slot for ``(email, org_uuid)``, or None when it cannot tell.

    Callers pass this to :func:`identity_for_config` so it never has to resolve
    an ADDRESS. `_resolve_account_identifier` raises on an address that names
    two slots, and that raise becomes a silent "leave the config alone" -- on
    the one roster shape the composite key exists for.

    None is honest: the caller then gets today's behaviour rather than a wrong
    slot.
    """
    if not email:
        return None
    try:
        return switcher._find_account_slot(
            switcher._get_sequence_data() or {}, email, org_uuid or "")
    except Exception:  # noqa: BLE001 — a lookup must not break a repair
        return None


def identity_for_config(switcher, email: str | None = None,
                        num: str | None = None) -> "dict | None":
    """The `oauthAccount` the config should name while a pin is set, or None.

    ``email`` asks about a DIFFERENT account than the one currently recorded.
    Two callers need it. The rollback, because by the time it runs the record
    has already been overwritten by the pin that failed; and ``set_pin``,
    because this argument is evaluated BEFORE ``apply_pin`` writes the record,
    so the no-argument form there resolves the outgoing pin.

    ``num`` is the slot a caller ALREADY resolved, and passing it skips the
    lookup entirely. That matters because `_resolve_account_identifier` RAISES
    when one address matches two slots -- cswap's own documented personal+org
    pattern -- and the function-wide except turns that into ``None``, which
    means "leave the config alone". So on exactly the roster the composite work
    exists for, the splice silently did nothing. `set_pin` says the same thing
    about its own re-derivation in its own comment; this is that lesson one
    function along.

    THE SEAM ANSWERS, THE SWITCHER ASKS. This used to live in switcher.py,
    where it resolved the pinned slot, read that slot's stored config and
    parsed the identity out — pin policy computed by cswap core, reaching a
    PRIVATE of this module to get started. Both halves were a boundary
    violation and the cswap session raised it.

    IT BELONGS HERE, NOT IN THE PACKAGE. The obvious destination was
    `cswap_pin.proxy`, and it is wrong: `_pinned_email_now` is documented to
    never ask the package, because the clear path must work when the package
    is exactly what is broken. The remaining steps read cswap's OWN backup
    store, which the package has no business knowing the layout of. Putting
    this there would invert the dependency instead of merely blurring it.

    None ON EVERY DOUBT — no pin, no stored config for it, an unreadable
    record. The caller then keeps the account being switched to, which is the
    behaviour that shipped for months. An optional feature must never be able
    to block a switch.
    """
    try:
        ident = pinned_identity(switcher)
        email = email or (ident[0] if ident else None)
        if not email:
            return None
        num = num or switcher._resolve_account_identifier(email)
        if not num:
            return None
        raw = switcher._read_account_config(str(num), email)
        oauth = json.loads(raw).get("oauthAccount") if raw else None
        if isinstance(oauth, dict) and oauth.get("accountUuid"):
            return oauth
        # A machine that has never switched INTO the pinned account has no
        # stored config to copy, and None here makes `_perform_switch` fall
        # back to the account being switched TO — so the pin never reaches
        # `~/.claude.json` and every bridge is owned by whoever is active.
        # The roster answers: its `uuid` IS the account uuid a stored config
        # calls `accountUuid`. A stored config still wins when it has one,
        # because it also carries displayName and organizationName, which
        # Claude Code writes and the roster has never held.
        row = ((switcher._get_sequence_data() or {})
               .get("accounts", {}).get(str(num)) or {})
        uuid = (row.get("uuid") or "").strip()
        if not uuid:
            # Claude Code compares an owner on account uuid AND org uuid, so an
            # identity without one is no better than None — and None at least
            # means "leave the field alone".
            return oauth if isinstance(oauth, dict) and oauth else None
        return {"emailAddress": row.get("email") or email,
                "organizationUuid": row.get("organizationUuid") or "",
                "accountUuid": uuid}
    except Exception:  # noqa: BLE001 — never block a switch
        return None


def _ask(name: str, *args):
    """Call `name` on the package, or None when it cannot be asked.

    The three passthroughs below were the same six lines three times. None
    means "cannot ask", never "asked and got nothing" — every caller treats
    those the same, so the collapse is honest. A caller that needs to tell
    them apart should not use these.
    """
    impl = _live_impl()
    if impl is None:
        return None
    try:
        return getattr(impl, name)(*args)
    except Exception:  # noqa: BLE001 — an optional extra cannot break a caller
        return None


def ca_path_for_trust():
    """The pin's CA bundle path, or None when the extra is absent.

    ONE GUARD, ONE FILE. This and the three below exist because four core
    modules imported `cswap_pin.proxy` directly, each re-implementing the same
    try/except. Behaviour was safe; the cost was that moving one package
    function meant editing five files, and that the seam this module exists to
    be had four holes around it.

    None is "cannot ask", never "asked and got nothing" — every caller here
    treats the two the same, so the collapse is honest. A caller that needs to
    tell them apart should not use this.
    """
    return _ask("ca_path_for_trust")


def live_bridge_names() -> "dict[str, str] | None":
    """Bridge id -> the name its session wants, or None when unavailable."""
    return _ask("live_bridge_names")


def titles_to_restore(sessions, names) -> "list | None":
    """Bridges the server titles wrongly, or None when the extra is absent.

    THE POLICY STAYS IN cswap-pin — this only carries the call across. What
    to touch is the package's decision, and deliberately not repeated here.
    """
    return _ask("titles_to_restore", sessions, names)


def pin_is_applying(switcher) -> bool | None:
    """Whether the daemon serving right now can actually mint the pinned token.

    THE SECOND QUESTION, and the one nothing asked. `pinned_email` answers
    "which account is it SET to", and every indicator the owner sees was lit on
    that alone: the TUI badge, the statusline, and `pin-coherence` — settings,
    proxy.json, pid and port all agreeing. Measured on the owner's laptop, all
    three read healthy while every request went out UNPINNED, because the daemon
    could not reach the macOS keychain and had marked its own record
    `unpinnable`. Nothing in this package had ever read that flag.

    The proxy's own comment says where that ends: reusing such a daemon "makes
    `cswap pin` report success forever while Remote Control sessions keep
    landing on the wrong account."

    ``False`` means SET BUT NOT APPLYING — the state worth shouting about.
    ``None`` is "cannot tell" (no extra, no daemon record, an unreadable one)
    and must read the same as healthy at the call sites: a badge that fires on
    "I could not look" is one people stop reading, which is the rule
    :func:`pin_is_broken` already follows.
    """
    impl = _live_impl()
    if impl is None:
        return None
    try:
        state = impl.read_daemon_state(_certdir(switcher))
    except Exception:  # noqa: BLE001 — a badge must not take the view down
        return None
    if not state:
        return None
    return not state.get("unpinnable")


def repin_current(switcher) -> bool:
    """Re-apply the pin already recorded, to replace a daemon that cannot mint.

    THE REPAIR THE PRODUCT ALREADY KNEW HOW TO DO. When a daemon publishes
    `unpinnable` the pin is set, the account is fine, and the daemon is serving
    — it simply cannot read the credential, so every request goes out on the
    active account. `heal` declines this by design ("something IS serving"), so
    the state persisted until a human ran `cswap pin <n>` by hand.

    That hand-run command lands here: `apply_pin` ends in
    ``return ensure_proxy(switcher) is not None``, and `ensure_proxy` reads the
    daemon record WITH a fingerprint — the read an `unpinnable` daemon answers
    "nothing is serving" to. So it spawns a successor, and a successor born
    somewhere that CAN read the credential mints again.

    Returns False on anything unexpected. A repair that raises is worse than a
    pin that stays broken: it takes down whatever asked for it.
    """
    impl = _live_impl()
    if impl is None:
        return False
    try:
        pin = impl.load_pin(switcher.backup_dir)
        if not pin:
            return False
        email, org = pin[0], (pin[1] if len(pin) > 1 else None)
        # NAME THE PIN IN THE LIVE CONFIG, exactly as `set_pin` does. Without
        # `identity=` the parameter defaults to None and the splice returns
        # early, so the repair restores a serving daemon while `~/.claude.json`
        # still names whichever account is active - and Claude Code takes that
        # field as the OWNER of every bridge it mints afterwards, which is the
        # teardown the splice exists to prevent.
        #
        # ASK ABOUT `email`, the account this call is re-pinning. The bare form
        # reads the RECORD, which `load_pin` above read too, so it happens to
        # agree here — and that is an accident of two readers sharing a file,
        # not a property of this function. `set_pin` had the same shape and was
        # WRONG, because there the record had not been written yet. Say which
        # account is meant and the question stops depending on who else read
        # what.
        return bool(impl.apply_pin(switcher, email, org,
                                   identity=identity_for_config(
                                       switcher, email=email,
                                       num=_slot_for(switcher, email, org))))
    except Exception:  # noqa: BLE001 — a repair must not take its caller down
        return False


# -- launch integration ------------------------------------------------------


# -- wiring removal ----------------------------------------------------------
#
# This half deliberately does NOT live in the optional package.

_WIRE_MARK = "_cswapPinWiredKeys"

# The launch path calls this on every `cswap run`, so its lock wait has to be
# bounded by something far below the 9s default: a user who never installed
# the pin must not wait on Claude Code's config lock at all, and one who did
# must not wait long. Nothing is lost by giving up — an unremoved wiring is
# retried on the next launch, and the caller fails open either way.
_LAUNCH_LOCK_BUDGET_S = 0.5

# The same reasoning for the SERVING probe on that path. A refused connect on
# loopback comes back in microseconds, so this only ever bites on a port that
# accepts nothing and answers nothing — a firewall rule, a half-dead daemon —
# and there a 2s default would blow the launch budget four times over on the
# probe alone. Guessing "not serving" after 0.2s costs at worst one unwire the
# next launch redoes; guessing wrong the other way costs a stalled launch.
_LAUNCH_PROBE_S = 0.2


def _pinned_email_now(switcher) -> tuple[str, str] | None:
    """The pin record as cswap's OWN file has it, or None. Never the package.

    Both the clear and set paths need this and neither can ask ``cswap_pin``
    for it: the package is precisely what may be broken, and on the set path
    ``apply_pin`` has already written the record by the time a failure is
    known. ``settings.json -> remoteControl`` is cswap's file, so read it.
    """
    from claude_swap import settings as _s

    section = _s._read_raw(_s.settings_path(switcher.backup_dir)).get("remoteControl")
    if not isinstance(section, dict):
        return None
    email = section.get("pinnedEmail")
    if not email:
        return None
    # `or ""` to match the WRITER. cswap_pin.save_pin always writes
    # `org_uuid or ""`, so a record with no org key read back as None here
    # while the same record after a rollback read as "" — unequal, and
    # _restore_pin then reported a successful rollback as a failure. The
    # package's own load_pin already normalizes; this reader had diverged.
    return email, section.get("pinnedOrganizationUuid") or ""


def _safe(exc: object) -> str:
    """An exception rendered for display, with URL userinfo removed.

    Every failure renderer here interpolates ``str(exc)`` — into CLI output,
    into a TUI modal, into a MENU LABEL — and the text comes out of an
    optional third-party package the seam does not control. A proxy URL
    carrying ``user:secret@host`` in a message would reach the screen
    verbatim. No path here builds one; the scrub exists because the seam has
    no way to promise none ever will.

    USERINFO ONLY. This is not general redaction: a bearer token in a header
    dump or a ``password 'x'`` embedded in a package's own message passes
    through untouched. Only ``scheme://user:pass@host`` is recognized and
    scrubbed.
    """
    import re

    # scheme://userinfo@host  ->  scheme://***@host. Anchored on "://" so an
    # ordinary email address in a message is left alone.
    return re.sub(r"(?<=://)[^/\s@]+@", "***@", str(exc))


def _rollback_tail(rolled: bool, before, email: str) -> str:
    """How a failed set_pin ended, in the record's own terms.

    Both failure branches say the same three things and said them twice.
    """
    if not rolled:
        return f"and the record may still name {email}, check with `cswap pin`"
    return "the previous pin is unchanged" if before else "nothing is pinned"


def _restore_pin(switcher, before: tuple[str, str] | None) -> bool:
    """Put the record back the way ``before`` had it. True when it IS back.

    The verdict is MEASURED, never inferred from the restore call: when
    ``_impl()`` itself is what raised, ``apply_pin`` never ran and the record
    was never touched, so a message claiming "may still name <email>" was
    telling the user to go check a state the code could already disprove.
    """
    # RESOLVED BEFORE THE CALL THAT DESTROYS ITS INPUT, in its own guard.
    # `apply_pin(None, None)` drops the pin record, and `_live_login_identity`
    # un-splices only while the config still equals that record — so asking
    # afterwards returns the account whose pin just failed. The separate `try`
    # is load-bearing: naming the config is best-effort, restoring the RECORD
    # is the job, and a raising lookup sharing the guard below skipped
    # `apply_pin` entirely.
    _back = None
    if not before:
        try:
            _back = _live_login_for_config(switcher)
        except Exception:  # noqa: BLE001 — a name must not cost the rollback
            _back = None
    try:
        _impl().apply_pin(switcher, *(before or (None, None)))
        # AND THE CONFIG, which the record alone does not put back. `apply_pin`
        # splices `~/.claude.json` BEFORE it starts the proxy, so a pin that
        # failed to start has already written its account there; restoring only
        # the record leaves Claude Code minting every later bridge under the
        # account whose pin just failed. Asked about `before` explicitly,
        # because the record still names the failure at this point.
        #
        # WHOSE NAME GOES BACK. Restoring a previous pin means that pin;
        # restoring NOTHING means the live login, exactly as `clear_pin`
        # decides it. Without the second case a failed FIRST pin left its own
        # account named in a config the record no longer mentions and nobody is
        # logged in as — the command reported failure and handed the machine to
        # it anyway.
        if before:
            # Safe to ask now: `apply_pin` has just RESTORED this record, so
            # the lookup sees the state it is naming. The None case cannot,
            # which is why it is resolved above.
            _back = identity_for_config(
                switcher, email=before[0],
                num=_slot_for(switcher, before[0], before[1]))
        _impl().splice_config_identity(_back)
    except Exception:  # noqa: BLE001 — the re-read below is the verdict
        pass
    return _pinned_email_now(switcher) == before


def _clear_pin_record(switcher) -> None:
    """Drop ``remoteControl`` from settings.json. Never raises.

    Only for the path where the package cannot do it — normally ``apply_pin``
    owns this file's pin section, and going around it would race the daemon's
    own writes. Here there IS no package, so nothing else can.
    """
    from claude_swap import settings as _s

    try:
        path = _s.settings_path(switcher.backup_dir)
        raw = _s._read_raw_for_write(path)
        if raw.pop("remoteControl", None) is not None:
            _s.atomic_write_json(path, raw)
    except Exception:  # noqa: BLE001 — the caller re-reads and reports
        pass


# -- where the receipt lives -------------------------------------------------
# The pin writes proxy vars into `.claude.json`'s `env` block and needs a
# receipt saying WHICH keys are its own, so unwiring restores exactly what it
# displaced and touches nothing else.
#
# THE ENV BLOCK CANNOT MOVE. Claude Code reads it out of `.claude.json` at
# boot; that file IS the interface.
#
# THE RECEIPT CAN, and should: it is bookkeeping only cswap reads, and
# `.claude.json` is the user's file — every key we leave in it is one more
# thing a human editing that file can trip over, and two of them
# (`_cswapPinWiredKeys`, `_cswapPinWiredKeysSaved`) are opaque unless you know
# this code. READ BOTH, WRITE NEW. The sidecar is authoritative when present;
# the config is still read for the copy every existing install has. A pin OLDER
# than this change keeps writing the config key, and it must keep working — so
# this is not a migration with a cutover, it is two readers and one writer, and
# the old location stays readable indefinitely.


def _wiring_present(_switcher) -> bool:
    """Does either config still carry a pin wiring?

    The companion to :func:`clear_wiring`'s return value, which cannot answer
    this: it returns False both for "there was nothing to remove" and for "the
    lock was contended so this path was skipped", and only the second is a
    failure. Read without a lock — a stale read here costs a re-run, while
    waiting on the same lock that just failed costs the command.

    ``_switcher`` is unused: both configs resolve from ``claude_swap.paths``,
    not from the switcher instance. Kept (and underscore-prefixed rather than
    dropped) so every call site stays symmetric with :func:`clear_wiring` and
    :func:`_wired_port_is_serving`, which the pin CLI/TUI pass a switcher to
    interchangeably — dropping it here alone would make this one predicate
    look different from its siblings for no reason a caller could see.
    """
    # ONE TRAVERSAL, in `wired_config_paths`. The getter-raises guard, the de-
    # dup and the unreadable-is-not-wired rule all live there now; see that
    # function for why `heal` cannot afford a raise to escape.
    return bool(wired_config_paths(_switcher))


def wired_env_keys(_switcher=None) -> dict:
    """``{config path: the env keys its receipt names}`` — read BEFORE a clear.

    THE MARKER CANNOT ANSWER "DID IT SURVIVE", because the marker is one of
    the things a clear removes. `purge` asked `wired_config_paths` afterwards
    and got an empty list for two opposite reasons: the wiring really went, or
    the RECEIPT went and left the wiring behind. On a sidecar-era wiring
    (receipt in ``<backup>/pin-wiring/<sha>.json``, nothing but env vars in
    the config) with an unwritable config dir, the second is what happens —
    `clear_pin` clears the writable sidecar, the config then reads as unwired,
    and purge printed "Removed: Cloud pin wiring" with no warning while
    ``HTTPS_PROXY`` and ``CSWAP_PIN_PORT`` still named a dead port. Measured.

    So the survivor question has to key on what the receipt NAMED, captured
    while the receipt still exists. This is that capture; `env_keys_survive`
    is the matching read afterwards.
    """
    keys = {}
    for path in wired_config_paths(_switcher):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — unreadable: nothing to promise about
            continue
        mark = _wire_mark_of(raw, path)
        if mark:
            keys[path] = list(mark)
    return keys


def _env_of_config(path) -> "dict | None":
    """The ``env`` block one config names, ``{}`` if it has none, None if the
    file cannot be read.

    The three are distinct for `env_keys_survive`: "I cannot check it" must not
    render as "it is clean" in the one message a purged user still gets.
    """
    try:
        env = json.loads(path.read_text(encoding="utf-8")).get("env")
    except Exception:  # noqa: BLE001 — unreadable: no opinion
        return None
    # A DICT OR NOTHING. A hand-edited `"env": "HTTPS_PROXY"` makes the
    # caller's `n in env` a SUBSTRING test, which reports a survivor over a
    # config that has no env block at all — the opposite failure to the one
    # above, and out of the same message.
    return env if isinstance(env, dict) else {}


def env_keys_survive(before: dict) -> dict:
    """``{config path: the keys still in its env}``, for what `wired_env_keys`
    captured. Empty when every clear did what it said.

    ASKS THE ENV BLOCK, not the marker — the marker is gone by now either way,
    and the env block is the thing that actually strands a launch. A config
    that has become unreadable counts as surviving: "I cannot check it" must
    not render as "it is clean" in the one message a purged user still gets.
    """
    left = {}
    for path, names in before.items():
        env = _env_of_config(path)
        if env is None:
            left[path] = list(names)      # unreadable is not clean
            continue
        still = [n for n in names if n in env]
        if still:
            left[path] = still
    return left


def wired_config_paths(_switcher=None) -> list:
    """Every config that still carries OUR marker, in read order.

    :func:`_wiring_present` answers "is any of them wired" and throws away
    WHICH — fine for a gate, wrong for a message. `purge` printed
    ``get_global_config_path()`` after asking that gate, so when the survivor
    was the OTHER config the user was sent to a file that was already clean
    while the wiring that strands them sat in one they were never told about.
    After a purge the record, cert dir and daemon state are gone, so hand
    editing is the only cure left and naming the wrong file is the whole
    failure.

    Same traversal, same guards, same de-dup as ``_wiring_present`` — it is
    now written once here and that predicate reads this.
    """
    wired = []
    for path in _each_config():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — unreadable/absent is not "wired"
            continue
        if _wire_mark_of(raw, path) is not None:
            wired.append(path)
    return wired


# -- command -----------------------------------------------------------------


# -- the operations, shared by the CLI and the TUI ---------------------------
#
# THE VERDICT LIVES HERE, NOT AT EACH CALL SITE. One decision implemented twice
# diverges: a fix lands on the CLI and its sibling in tui/dashboard.py keeps the
# old behaviour, and the two front ends disagree without either looking wrong on
# its own.
#
# So these return a (ok, message) the caller only has to render. A divergence
# now needs someone to write a second copy of the logic rather than to forget a
# line.


def clear_pin(switcher) -> tuple[bool, str]:
    """Remove the pin AND its wiring. ``(ok, message)``.

    Both halves are re-read afterwards rather than inferred: ``apply_pin``
    cannot report on the wiring, and ``clear_wiring``'s bool is False both for
    "nothing to remove" and for "the lock was contended so this path was
    skipped" — only the second is a failure, and the skip is deliberate.
    """
    had_pin = _pinned_email_now(switcher) is not None
    # CAPTURED BEFORE THE FIRST THING THAT UNWIRES, which is `apply_pin`, not
    # `clear_wiring`. This snapshot sat below both, so the survivor check ran
    # against a config the package had ALREADY rewritten: a peer that removed
    # the receipt but left the env keys (a partial rewrite, or the
    # ledger-first/config-second split failing on the second half) produced an
    # empty `before`, an empty `survivors`, and `(True, 'Unpinned the cloud
    # account')` over a config still naming a dead proxy port.
    #
    # `purge` gets this right by capturing before it calls US; the old comment
    # claimed parity with it, and was only true relative to `clear_wiring`.
    before = wired_env_keys(switcher)
    # WHOSE IDENTITY THE CONFIG CARRIES AFTERWARDS. The package cannot work
    # this out — resolving an account to its stored identity means reading
    # cswap's backup store — so the clear hands it over, the same split as
    # `set_pin`. `_live_login_identity` un-splices, which is the point: the
    # account that has been serving is the one the config should name once the
    # pin is gone. None on any doubt leaves the field alone, and the next
    # switch rewrites it; a blank owner would be worse than a stale one.
    try:
        _back_to = _live_login_for_config(switcher)
    except Exception:  # noqa: BLE001 — nothing optional may block a clear
        _back_to = None
    try:
        impl = _impl()
        impl.apply_pin(switcher, None, None, identity=_back_to)
    except Exception:  # noqa: BLE001 — this command must work when the pin does not
        # THE RECORD IS CSWAP'S OWN FILE, so clear it here rather than
        # reporting that the package could not. With the extra uninstalled,
        # leaving it meant `--clear` failed, told the user to REINSTALL the
        # package they had just removed (advice that inverts their intent and
        # never converges — run 2 is identical), and then re-pinned the old
        # account the moment anything reinstalled it, live, with no user
        # action. Same for a too-old or broken-root package.
        #
        # This is the stranding clear_wiring was moved into this repo to
        # prevent, one level up: the wiring is cswap's file and gets cleared,
        # and settings.json -> remoteControl is equally cswap's file.
        _clear_pin_record(switcher)
    # AND THE SAME FALLBACK WHEN IT DID NOT RAISE. A peer whose `apply_pin`
    # RETURNS and clears nothing reaches the dead end the branch above exists
    # to prevent, without going through it: the record is still there, the re-
    # read below says "still pinned", and the user is told to "re-run once it
    # frees up" — advice that never converges, which is the exact wording the
    # comment above rejects. An older peer, or one whose pin backend is off,
    # does precisely this.
    if _pinned_email_now(switcher) is not None:
        _clear_pin_record(switcher)
    cleared = clear_wiring(switcher)
    still_pinned = _pinned_email_now(switcher) is not None
    # THE ENV BLOCK, NOT THE MARKER. `_clear_wiring_locked` returns
    # `_clear_ledger(path)` AFTER the config write, so an unwritable
    # `pin-wiring/` (root-owned parent, read-only mount, full disk) reported
    # "could not remove the wiring; re-run once it frees up" FOREVER over a
    # user whose launches were already fine. Its docstring argues the return
    # prevents a phantom success; it substituted a permanent phantom failure,
    # which is the same defect with the sign flipped.
    survivors = env_keys_survive(before)
    if still_pinned or survivors:
        what = " and ".join(
            w for w, on in (("the pin", still_pinned), ("the wiring", bool(survivors)))
            if on
        )
        return False, f"Could not remove {what}; re-run once it frees up"
    stale = wired_config_paths(switcher)
    if stale:
        # A DIFFERENT STATE AND A DIFFERENT SENTENCE. Nothing dials a dead
        # port any more; only cswap's own bookkeeping is stuck, and a re-run
        # cannot rewrite a directory it may not write. Name the file.
        return True, (
            "Removed the cloud pin wiring. A stale receipt could not be "
            "deleted; remove "
            + " and ".join(str(_ledger_path(p)) for p in stale)
            + " by hand, or cswap will keep reporting a wiring that is gone"
        )
    if not cleared and not had_pin:
        return True, "No cloud account pinned"
    return True, "Unpinned the cloud account"


def set_pin(
    switcher, email: str, org_uuid: str | None, num: str | None = None
) -> tuple[bool, str]:
    """Pin the cloud surface to ``email``. ``(ok, message)``.

    A failure ROLLS THE RECORD BACK: ``apply_pin`` writes ``remoteControl``
    before it starts the proxy, so reporting the failure while leaving it makes
    every read-back — ``cswap pin``, the TUI badge — contradict the message.

    ``num`` is the slot both call sites ALREADY resolved. Re-deriving it from
    the email here was a real bypass, not a tidiness point: cswap's own
    documented personal+org pattern gives one address two slots, so
    ``resolve_account(email)`` raises ``ConfigError`` and the API-key refusal
    below was skipped entirely — accepting exactly the account it exists to
    reject.
    """
    # REFUSED HERE, not at the call sites. An API-key account can never be
    # pinned — `sk-ant-api…` is not OAuth JSON, so the provider returns None
    # for every request and each one fails open: daemon spawned, badge lit,
    # nothing pinned, ever. The TUI's row filter is a courtesy, not the
    # enforcement: refresh_root_menu returns early below depth 1, so an open
    # submenu is never rebuilt while the snapshot keeps updating, and a row
    # that was OAuth when the menu was drawn pins an API-key account when it
    # is selected.
    #
    # A kind we cannot READ is not permission to proceed. Swallowing the
    # lookup turned an unreadable sequence.json into a silent skip of the
    # refusal — the failure mode is identical to having no refusal at all,
    # and it is invisible. Refuse loudly instead; the user can fix the store.
    if num is None:
        try:
            num = switcher.resolve_account(email)[0]
        except Exception as exc:  # noqa: BLE001
            return False, (
                f"Could not resolve {email} to one account ({_safe(exc)}), so the "
                "cloud pin cannot check it is not an API-key account"
            )
    try:
        kind = switcher._account_kind(num)
    except Exception as exc:  # noqa: BLE001
        return False, (
            f"Could not read what kind of account {email} is ({_safe(exc)}); the "
            "cloud pin needs an OAuth account and will not guess"
        )
    if kind == "api_key":
        return False, (
            f"{email} is an API-key account, which the cloud pin cannot "
            "use: Remote Control and Artifacts need an OAuth bearer"
        )
    before = _pinned_email_now(switcher)
    try:
        # HAND OVER THE IDENTITY, do not apply it here. Once a pin is set the
        # live config must name it — `oauthAccount` is what Claude Code reads
        # to decide who OWNS a bridge — and that rule is pin functionality, so
        # the package owns it. What cannot move is this LOOKUP: it reads
        # cswap's backup store, whose layout the package has no business
        # knowing (see `identity_for_config`). So cswap looks it up and the
        # package applies it, which is the split this seam exists for.
        #
        # ASK ABOUT `email`, NOT ABOUT THE RECORD. Python evaluates this
        # argument BEFORE `apply_pin` runs, and `apply_pin` is what writes the
        # record — so the no-argument form resolves the PREVIOUS pin: None on a
        # first pin (nothing splices, the pin is inert) and the outgoing
        # account on a re-pin (the config names what was just unpinned).
        started = _impl().apply_pin(
            switcher, email, org_uuid,
            identity=identity_for_config(switcher, email=email, num=num))
    except Exception as exc:  # noqa: BLE001 — a traceback tells a user nothing
        rolled = _restore_pin(switcher, before)
        return False, (
            f"Could not pin the cloud account: {_safe(exc)}. "
            + _rollback_tail(rolled, before, email)
        )
    if not started:
        # SAME DEFECT AS THE RAISE PATH, sibling branch. apply_pin writes the
        # record before starting the proxy, so leaving it here made the two
        # commands contradict each other: `cswap pin 2` said "nothing is
        # pinned yet" and exited 1 while `cswap pin` then printed the address
        # and exited 0, with the ○ cloud badge lit. Roll back to whatever was
        # pinned before, exactly as a raise does.
        rolled = _restore_pin(switcher, before)
        return False, (
            f"Could not pin the cloud account to {email}: no proxy is running, "
            "so nothing is pinned yet. " + _rollback_tail(rolled, before, email)
        )
    return True, f"Pinned the cloud account (RC/artifacts) to {email}"


def _wired_ports() -> list[int]:
    """Every pin port the configs name, in read order. Unreadable ones are
    absent rather than zero — "no opinion" and "port 0" are different facts.

    For "is ANYTHING wired at all" questions (``_wiring_present``,
    ``clear_wiring``, the every-config-must-serve probe below) where both
    configs' opinions genuinely apply at once.
    """
    return [port for path in _each_config() if (port := _port_of_config(path))]


def _wired_port_is_serving(_switcher, connect_timeout: float = 2.0) -> bool:
    """Is the port the CONFIG names actually answering?

    REVIEWED AND KEPT AS IS, so the next reader does not re-raise it. A review
    counted "up to five probes per `--ensure`, 10 x 0.2s = 2.0s on every
    hand-launched claude" and proposed computing one result and threading it
    through. The arithmetic is close and the disposition is wrong:

      - At most FOUR rounds can run in one pass, not five: two
        `_wired_port_is_serving` inside `heal` and one `_dead_wired_configs`
        each in `heal` and `run`. The third `_wired_port_is_serving` is on the
        `elif` branch and is mutually exclusive with the other two.
      - Every repeat sits AFTER something that can change the answer. 1654 runs
        after `impl.heal()`; 1663 exists because "the restart may have
        succeeded while returning False — re-READ rather than infer"; `run`'s
        scan exists because a contended lock can leave `heal`'s verdict stale.
        Each of those comments carries its own measurement of the damage that
        followed inferring instead of re-reading. Memoising is precisely the
        inference they forbid.
      - The cost needs a port that DROPS. A loopback port with nothing on it
        REFUSES instantly, so the ordinary dead-pin case costs microseconds;
        0.2s per config requires a deliberate firewall rule on 127.0.0.1.

    So the price is paid only in a state that barely occurs, and it buys the
    one property that has already stopped this code from unwiring a live pin
    twice. If it ever needs to go, the thing to remove is a re-read, and that
    needs a measurement showing the state cannot change across it — not a
    cache.

    Asks the thing that is about to be removed, rather than any state file.
    ``proxy.json`` is unlinked at the START of a respawn, so its absence is not
    proof of death while the original daemon is still serving — deciding from
    the record alone has already unwired a live pin once.

    Works with the extra absent or broken: it is a loopback connect, not an
    import. That matters because the uninstalled case is exactly when a user
    can least afford a wrong answer in either direction.

    False when nothing is wired, when the port is unreadable, or when it
    refuses — all of which mean "healing is allowed to proceed".

    ``_switcher`` is unused (see :func:`_wiring_present`) but kept, and
    underscore-prefixed, for the same call-compatibility reason.
    """
    # EVERY WIRED CONFIG MUST SERVE, not merely one of them.
    #
    # The two configs are written asymmetrically:
    # `cswap_pin.wire_global_config` writes only the session config, while this
    # reads both — the same asymmetry `clear_wiring` documents as its reason
    # for clearing both. Answering on the FIRST config that serves lets a live
    # session config mask a DEAD default config, so plain `claude` from a
    # terminal boots against the dead one while `--heal` says "Nothing to
    # heal":
    #
    #     session cfg -> 42967 (LIVE)   default cfg -> 39967 (DEAD)
    #     _wired_port_is_serving : True      <- OR over both
    #     heal()                 : (False, "Nothing to heal")
    #     default cfg still names the dead port: True
    #
    # An unwired config is not a counter-example — it sends nobody anywhere.
    # Only a config that NAMES a port has an opinion, and every such opinion
    # has to be right for the pin to be serving.
    ports = _wired_ports()
    return bool(ports) and all(
        _port_answers(port, connect_timeout) for port in ports
    )


def _nothing_to_heal(switcher) -> tuple[bool, str]:
    """The healthy verdict — unless a wired config cannot be read at all.

    THREE EXITS SAID "Nothing to heal" AND ONLY ONE OF THEM ASKED. A config
    carrying the marker with no readable `CSWAP_PIN_PORT` is deliberately not
    condemned (see `_dead_wired_configs`' second guard: "I cannot tell" is not
    "it is dead"), but declining to ACT is not a reason to report the
    all-clear, and `cswap pin --heal` is where this module's own messages send
    a stranded user.

    PER CONFIG, NOT MACHINE-WIDE. The first version asked
    `_wiring_present(...) and not _wired_ports()` — both "does ANY config" —
    so a default config on a LIVE port hid a session config whose own port was
    hand-edited. It also sat at only the LAST of the three exits, and the
    serving branches above return first in exactly that scenario, so the check
    could not run where it was needed.

    AND IT NAMES THE FILE. "somewhere a config is unreadable" sends the user
    to grep two paths; `purge` learned that the expensive way.
    """
    unreadable = [
        path for path in wired_config_paths(switcher)
        if _port_of_config(path) is None
    ]
    if not unreadable:
        return False, "Nothing to heal"
    # TWO STATES REACH THAT LIST AND THEY NEED DIFFERENT SENTENCES. A config
    # whose receipt names keys the env no longer has is not a broken port
    # value — it is a LEFTOVER RECEIPT, and telling the user to fix a
    # `CSWAP_PIN_PORT` that is not in the file is the same defect this
    # message was added to remove.
    #
    # DETERMINISTIC, not rare: `_ledger_path` keys the sidecar on the config
    # PATH, so a `.claude.json` deleted and recreated at the same path
    # inherits the same receipt — which is what Claude Code recreating that
    # file normally does. The `--clear` half of the advice does resolve it,
    # which is what keeps the cost to one message.
    # HOISTED, because it is loop-invariant and expensive. Called inside the
    # comprehension it re-walked both configs and both sidecars once per
    # unreadable path, on a file this module's own comment calls "megabytes on
    # a real machine".
    receipts = wired_env_keys(switcher)
    stale = [p for p in unreadable
             if not (set(receipts.get(p, ())) & set(_env_of_config(p) or ()))]
    if stale:
        return False, (
            "A leftover cloud pin receipt names "
            + " and ".join(str(path) for path in stale)
            + ", whose env block no longer carries the wiring, so nothing is "
            "misrouted. Run `cswap pin --clear` to drop the receipt"
        )
    return False, (
        "A cloud pin wiring names no readable CSWAP_PIN_PORT in "
        + " and ".join(str(path) for path in unreadable)
        + ": it is left alone (it may still be serving) and cannot be "
        "checked. Fix that value, or run `cswap pin --clear` to remove the "
        "wiring"
    )


def heal(
    switcher, *, connect_timeout: float = 2.0,
    lock_timeout: float | None = None,
) -> tuple[bool, str]:
    """Make the pin serving again, or make it harmless. ``(changed, message)``.

    ``lock_timeout`` bounds OUR config lock, and reaches the PACKAGE's config
    lock too on a version that accepts it. What it does not reach is
    cswap-pin's SPAWN lock, which `impl.heal` takes with no timeout of ours to
    give it, so a repair that has to spawn waits for whoever is already
    spawning. Bounded in practice -- that holder is performing the repair, and
    flock releases on its death -- but no budget here covers it.

    A DEAD PIN MUST NOT TAKE THE SESSION WITH IT. Everything else here reacts
    to a launch, so when the daemon dies while sessions are up nothing brings
    it back — and the stale wiring in ``.claude.json`` is applied at BOOT, so
    new sessions inherit the dead port too and cannot start either: every
    session on the box shows ``Unable to connect to API (ConnectionRefused)``
    while the proxies behind the pin are healthy, and only a hand re-pin
    clears it.

    Two outcomes, in order of preference:

    1. Restart the daemon on the SAME port. Live sessions are already wired to
       that address and their env is fixed at exec, so a daemon returning to it
       is picked up with no restart and nothing to reconnect.
    2. Failing that, REMOVE THE WIRING. Unpinned is a working session; wired to
       a dead port is not. The fallback the shell provides (the corporate proxy,
       or nothing) is what the user had before they ever pinned.

    Never raises. NOT because a timer calls it -- nothing does, and this
    docstring claimed a status-line caller for long enough that a review
    re-tuned budgets for one. Its two callers are `cswap pin --ensure`, which
    an rc file runs before every hand-launched ``claude``, and a hand-run
    ``cswap pin --heal``. Both are a LAUNCH or a repair someone is waiting on,
    and a health check that can end either is worse than the fault it reports.

    ``connect_timeout`` is every loopback probe below, and it is a keyword the
    LAUNCH path must pass. The default is right for a hand-run ``--heal``, and
    wrong for ``--ensure``: that hook runs from an rc file before every
    hand-launched ``claude``, and one call arms three probes here, so a port
    that black-holes rather than refuses costs the launch 4.2s on the default.
    Same defect the two call sites in this file already carry a budget for —
    it was only invisible here because it lives one frame down.
    """
    # A SERVING PIN IS NEVER **TORN DOWN**. That is what the guard protects,
    # and the destructive operation is `clear_wiring` at the bottom — not the
    # restart. Returning here on `serving`, before `impl.heal()` runs, makes a
    # whole class of repair unreachable:
    #
    #   a daemon SERVING its wired port while running code we no longer ship
    #
    # is exactly the state an upgrade leaves behind, and it answers "Nothing to
    # heal" forever. A wiring that names a DEAD port recycles, but that is the
    # right outcome for the wrong reason.
    #
    # So the restart runs FIRST and the serving check gates only the unwire.
    # `impl.heal` is safe to call in the serving case by construction: it
    # returns False for "serving, wired, and current" and recycles only when
    # the fingerprint says the daemon predates the installed code — rebinding
    # the SAME port, so live sessions never see the swap.
    impl = _live_impl()
    if impl is not None:
        try:
            # Covers THREE halves now: restart a daemon that died, re-wire a
            # daemon that is serving while the config names nothing, and
            # recycle one that is serving but obsolete. The second is the state
            # a recovery leaves behind; the third is the state an upgrade does.
            #
            # RE-READ THE TRUE AS WELL AS THE FALSE. The branch below already
            # refuses to infer an outage from a False; trusting a True was the
            # same mistake pointing the other way, and this function's whole
            # thesis is that a verdict comes from the state, not from a call.
            # It matters because the package is a PEER on its own release
            # schedule (see _impl): the seam cannot promise what a future
            # version returns. An impl that returns True while binding nothing
            # gives `heal() -> (True, "Restored the cloud pin")` with the wired
            # port not serving, so the launch reports a repair that did not
            # happen while every session it starts dials a dead port.
            # THE OWNER FIELD RIDES ALONG. The package owns that behaviour and
            # documents it; the host's part is only that it must LOOK THE
            # IDENTITY UP rather than let the package read it, because the
            # value lives in cswap's per-slot config backup and the package
            # has no business knowing that layout.
            #
            # ASKED, NOT ASSUMED. The package is a peer on its own release
            # schedule, and one that predates this argument raises TypeError
            # on the keyword — inside this try, which would swallow it and
            # silently lose heal altogether. `signature` answers whether this
            # version takes it instead of catching the symptom, so a TypeError
            # raised INSIDE heal is not mistaken for an old signature and
            # retried against a function that already ran.
            # NEITHER VIEW ALONE IS SAFE. A `wraps` wrapper that DROPS
            # keywords looks accepting when followed; a transparent `(*a,
            # **kw)` one looks accepting when unfollowed. Both lose heal to a
            # swallowed TypeError. `**kwargs` counts as accepting because a
            # signature cannot say otherwise -- an assumption about the
            # callee, and a version forwarding kwargs to something stricter
            # still raises. No signature test can see that one.
            def _accepts(sig, name: str) -> bool:
                params = sig.parameters
                return name in params or any(
                    p.kind is inspect.Parameter.VAR_KEYWORD
                    for p in params.values())

            def _both(name: str) -> bool:
                # THE UNFOLLOWED VIEW WINS WHEN IT NAMES THE PARAMETER, because
                # that is the signature the call actually binds against: a
                # compat shim that GROWS a keyword in a wrapper over its older
                # inner accepts it, and following `__wrapped__` reports the
                # inner, which does not. Requiring both views to agree can only
                # turn a yes into a no, so every disagreement it invents is a
                # carry dropped in silence.
                #
                # The agreement is still what decides a wrapper that says only
                # `**kwargs` -- there the outer claims nothing, so the inner is
                # the only evidence there is.
                try:
                    outer = inspect.signature(impl.heal, follow_wrapped=False)
                    if name in outer.parameters:
                        return True
                    return (_accepts(inspect.signature(impl.heal), name)
                            and _accepts(outer, name))
                except (TypeError, ValueError):
                    return False

            _takes_identity = _both("identity")
            # THROUGH `_slot_for`, like every other caller. The bare form
            # makes `identity_for_config` resolve an ADDRESS, and
            # `_resolve_account_identifier` RAISES when one address names two
            # slots -- the documented personal+org roster. The function-wide
            # except turns that into None, the package leaves the field alone,
            # and the drift this carry exists to stop continues untouched on
            # exactly the roster the composite key was built for.
            #
            # AND THE BUDGET RIDES WITH IT, or the package waits ten times as
            # long as this caller allows itself: `lock_timeout` bounds OUR
            # config lock, the splice inside `heal` takes the SAME lock, and
            # it had no way to hear about it.
            _kw = {}
            if _takes_identity:
                _pin_id = pinned_identity(switcher) or (None, None)
                _kw["identity"] = identity_for_config(
                    switcher, email=_pin_id[0],
                    num=_slot_for(switcher, _pin_id[0], _pin_id[1]))
            if lock_timeout is not None and _both("lock_timeout"):
                _kw["lock_timeout"] = lock_timeout
            _healed = impl.heal(switcher.backup_dir, **_kw)
            if _healed and _wired_port_is_serving(
                switcher, connect_timeout=connect_timeout
            ):
                return True, "Restored the cloud pin"
        except Exception:  # noqa: BLE001 — fall through to the safe outcome
            pass
        # The restart may have succeeded while returning False (it also uses
        # False for "already serving"). Re-READ rather than infer: unwiring a
        # pin that just came back is the same damage as unwiring a live one.
        if _wired_port_is_serving(switcher, connect_timeout=connect_timeout):
            return _nothing_to_heal(switcher)
    elif _wired_port_is_serving(switcher, connect_timeout=connect_timeout):
        # No package, so nothing can restart OR recycle — but a serving pin is
        # still a working one, and removing its wiring would unpin a healthy
        # session. The guard has to survive the package being absent, which is
        # exactly when a user can least afford a wrong answer.
        #
        # The port the WIRING names is the right question, not any state file:
        # `_spawn_daemon` unlinks proxy.json as its first act, so a missing
        # record is not proof of death while the original daemon still serves.
        return _nothing_to_heal(switcher)
    # No package, or the restart failed. Either way the wiring must not outlive
    # the daemon it points at. clear_wiring works WITHOUT the package on
    # purpose — the wiring is cswap's own record, and the case where the extra
    # is broken is exactly when a user cannot afford to be stranded.
    #
    # AND SAY WHICH OF THE TWO HAPPENED. `present and clear_wiring(...)`
    # collapsed "there was nothing to remove" into "I could not remove it", and
    # fell through to the healthy verdict for both. The second is reachable and
    # routine: the budget here is 0.5s and Claude Code holds the config lock
    # during a credential refresh. With the lock held, a wiring present and the
    # port dead, `heal` answers (False, "Nothing to heal") over an outage in
    # progress and the wiring survives. That is this file's signature defect,
    # in the channel that matters most: `heal` is what a launch runs to find
    # this, so during the exact failure it exists to report, the only signal
    # anyone had said everything was fine.
    #
    # RE-READ AFTER CLEAR_WIRING, exactly as clear_pin already does — its bool
    # is True when ANY of the two configs changed, not when BOTH did. With the
    # session config's lock held and the default config free, clear_wiring
    # clears the default and returns True for that one change, so `heal`
    # reports "Removed a cloud pin wiring" while the session config still names
    # the dead port.
    #
    # THE SAME QUESTION `_dead_wired_configs` ASKS, not `_wiring_present`
    # alone. `_wiring_present` keys on the marker only, so a config carrying
    # the marker with no readable CSWAP_PIN_PORT satisfied it and got torn down
    # here — the exact shape `_dead_wired_configs`' second guard (see its
    # docstring) declares must not be read as "the proxy is dead". `heal` is
    # the worse of the two call sites to leave unguarded: `--ensure` reaches it
    # before EVERY hand-launched claude, while `--heal` is the one-shot.
    try:
        # THE DEAD CONFIGS, NOT "THE WIRING". The list IS the same question
        # one bool wide, and asking it as a bool is what let a machine-wide
        # verdict authorise a machine-wide ACT: with the session config
        # live and the default config dead, `clear_wiring` stripped both and
        # unpinned sessions that were routed correctly. Take the list instead
        # and clear exactly what is dead — one probe round either way.
        dead = _dead_wired_configs(switcher, connect_timeout=connect_timeout)
        if dead:
            # BUDGETED PER CALLER, like `connect_timeout` beside it. Hardcoding
            # the launch budget here gave the HUMAN recovery command 0.5s:
            # Claude Code holds .claude.json.lock routinely during a credential
            # refresh, so `cswap pin --heal` — the command this function's own
            # message tells the user to run — bounced with "the config is
            # locked" where a patient wait would have taken it.
            #
            # CAPTURED BEFORE THE CLEAR, and against `dead` only. Two
            # corrections in one line, and the second is the sibling call site
            # `clear_pin` already got: AGAINST `dead`, not every config — a
            # live config left wired on purpose is not a survivor, and
            # `_wiring_present` counted it as one, reporting "could not be
            # removed" over a clear that did exactly what it meant to.
            #
            # AND THE ENV BLOCK, not the marker. `wired_config_paths` reads
            # `_wire_mark_of`, so a clear that rewrote the config but could not
            # rewrite the SIDECAR still saw a survivor and answered "could not
            # be removed — re-run" over a machine whose launches were already
            # fine. `clear_pin` was moved off the marker for exactly this;
            # leaving `heal` on it is the sibling left behind, and `heal` is
            # the worse one — its docstring makes the loudest claim about not
            # reporting a fault that is not there.
            wanted = {str(d) for d in dead}
            before = {
                p: keys for p, keys in wired_env_keys(switcher).items()
                if str(p) in wanted
            }
            clear_wiring(switcher, timeout=lock_timeout, only=dead)
            if not env_keys_survive(before):
                return True, (
                    "Removed a cloud pin wiring whose proxy was gone. "
                    "sessions fall back to the proxy they had before the pin"
                )
            # NAME THE CONDITION, NOT A CAUSE THIS CANNOT KNOW. `clear_wiring`
            # catches every exception around the lock, so one message covered a
            # held lock AND a config directory this user cannot write — and
            # asserted the first for both. On the permission shape the advice
            # that followed ("re-run `cswap pin --heal`") can never come true:
            # re-running chmods nothing, so the user waits on a lock that was
            # never held while the fix is one command away.
            #
            # AND SAY WHERE THE CAUSE IS. `clear_wiring`'s WARNING carries the
            # real exception, but `logging_config` attaches a console handler
            # under `--debug` alone — so on an ordinary run this line is the
            # whole of what the user sees, and it was pointing at the wrong
            # thing. Naming one cause for a condition with two fails the same
            # way as naming none.
            return False, (
                "A cloud pin wiring points at a proxy that is gone, and it "
                "could not be removed; re-run `cswap pin --heal`, or "
                "`cswap pin --heal --debug` for the reason (a held config "
                "lock and a config directory you cannot write both land here)"
            )
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not heal the cloud pin ({_safe(exc)})"
    # REFUSING TO ACT IS NOT A REASON TO REPORT THE ALL-CLEAR. A marker with no
    # readable `CSWAP_PIN_PORT` reaches here because `_dead_wired_configs`
    # declines to condemn it — correctly, "I cannot tell" is not "it is dead",
    # see its second guard. But declining leaves the wiring in place, and this
    # answered "Nothing to heal": the file's signature defect (the capitals
    # above) through a different door. `cswap pin --heal` is the command this
    # module's own messages send a stranded user to, and over a hand-edited or
    # out-of-range port — the case `_port_of_config`'s range check exists for —
    # it printed the all-clear.
    #
    # NOT A WOLF: today's writer always emits the port, so the normal wired
    # machine never reaches this branch. `--ensure` discards the message
    # entirely, so the launch path stays silent either way.
    return _nothing_to_heal(switcher)


def serving_port(switcher, *, connect_timeout: float = 2.0) -> int | None:
    """The port a live pin daemon is serving, or None. CSWAP'S OWN RECORD.

    Exists because nothing could ASK. Measured in the owner's dotfiles:
    `cc-update` opens ``pin-proxy/proxy.json`` at TWO hardcoded paths and
    parses our JSON schema, because a pinned session's ``HTTPS_PROXY`` names
    the pin's own dynamic port rather than the cache proxy's — and without
    that number every pinned session is reported as "the cache proxy was
    bypassed" while it is in fact chained correctly. A consumer that cannot
    ask reaches into our data dir, and then our LAYOUT and our SCHEMA become
    a compatibility surface we cannot change without breaking scripts we do
    not own.

    Read from the record rather than through the package, like
    :func:`_pinned_email_now`: the caller most likely to need this is one
    diagnosing a failure, which is exactly when the package may be the thing
    that is broken.

    THE DAEMON'S RECORD, NOT THE CONFIG'S. These are different questions and
    the caller is asking the first one: "which port is the proxy on", so that
    a session seen using it can be recognised as chained rather than reported
    as bypassing the cache proxy. The config's answer is "which port were
    sessions TOLD to use", which is the same number in the healthy case and
    deliberately not during a handover — `proxy.json` is what the daemon
    itself publishes.

    LIVENESS IS NOT ASSUMED. A recorded port whose daemon has died is the
    stranding case this module keeps meeting, and answering with it would
    send a caller to an address nothing serves. So the port is asked, not
    inferred: a loopback connect, which also works with the package absent.
    """
    import socket

    record = _certdir(switcher) / "proxy.json"
    try:
        port = int(json.loads(record.read_text(encoding="utf-8"))["port"])
    except Exception:  # noqa: BLE001 — absent/unreadable/malformed: no opinion
        return None
    if not 0 < port <= 65535:
        return None
    try:
        # BUDGETED BY THE CALLER. This was the one probe left hardcoded after
        # every other one on a per-tick path was given a budget. Its own
        # docstring names the consumer: any caller that runs on a timer
        # (a contract, not an observed one — see `--heal`'s block).
        # A port that DROPs rather than refuses — a firewall rule, a
        # half-dead daemon — then costs the full 2s on every tick, which is
        # exactly the cost `_LAUNCH_PROBE_S` exists to refuse one function up.
        with socket.create_connection(
            ("127.0.0.1", port), timeout=connect_timeout
        ):
            return port
    except OSError:
        return None


def run(
    switcher,
    account: str | None,
    clear: bool = False,
    heal_only: bool = False,
    get_port: bool = False,
    get_certdir: bool = False,
    set_port: int | None = None,
    ensure: bool = False,
) -> int:
    """Entry point for ``cswap pin``. Mirrors :func:`claude_swap.menubar.run`:
    the optional dependency is resolved here, at call time, not at import."""
    from claude_swap.printer import accent, dimmed, warning

    if ensure:
        # THE LAUNCH CONTRACT, which `--heal` deliberately does not make.
        # An rc hook calls this before EVERY `claude`, so three properties
        # matter more than the repair itself:
        #
        #   never fails    a launch is not optional and a pin is, so every
        #                  path exits 0 — including a raise, which an rc hook
        #                  would otherwise propagate into the launch
        #   silent         a launch that prints has changed what the user sees
        #                  for an optional feature
        #   cheap when idle  this runs on every launch, so a machine that
        #                  never pinned must not pay for the repair path
        #
        # This exists so 200 lines of shell in a user's dotfiles can be one
        # line. That script was written when `--heal` did not exist in the
        # installed cswap; the repair belongs here, and the only irreducible
        # part is the TRIGGER — a hand-launched `claude` execs from the user's
        # shell and nothing of ours runs inside it.
        try:
            # NOTHING WIRED AND NOTHING RECORDED IS THE COMMON CASE.
            if not _wiring_present(switcher) and _pinned_email_now(switcher) is None:
                return 0
            # BUDGETED, like the two probes below it.
            heal(switcher, connect_timeout=_LAUNCH_PROBE_S,
                 lock_timeout=_LAUNCH_LOCK_BUDGET_S)
            # RE-READ, DO NOT TRUST THE RETURN. The question is whether a port
            # is actually being served, and only the config and a connect can
            # answer it — `heal` calls into `cswap_pin`, a PEER
            # on its own release schedule, so a version that reports success
            # while binding nothing gives a launch hook that did its job and a
            # session that dials a dead port anyway. The wiring is CSWAP'S OWN
            # record, so removing it needs no package at all: unpinned is a
            # working session, wired-to-a-dead-port is not.
            #
            # THE PROBE IS BUDGETED TOO, not just the lock below. Its default
            # is 2.0s, and a port that black-holes instead of refusing pays all
            # of it — on the hook that runs before EVERY hand-launched
            # `claude`. `wire_launch_env` already passes `_LAUNCH_PROBE_S` here
            # for exactly this reason; this site did not.
            #
            # THE DEAD CONFIGS, NOT "THE WIRING" — same correction as `heal`
            # and `wire_launch_env`, on the site that runs from an rc hook
            # before EVERY hand-launched `claude`.
            #
            # NOT DEAD CODE just because `heal` ran above it: `heal` clears the
            # same dead set under `_LAUNCH_LOCK_BUDGET_S`, so a contended
            # config — Claude Code holding `.claude.json.lock` through a
            # credential refresh, which this file calls routine — leaves the
            # verdict stale and drops through to here. The lock that stopped
            # `heal` stops this clear too, so the config it CAN take is the
            # free one, which is the LIVE one.
            dead = _dead_wired_configs(switcher, connect_timeout=_LAUNCH_PROBE_S)
            if dead:
                # BUDGETED, like every other call on a launch path.
                # `_LAUNCH_LOCK_BUDGET_S` exists for exactly this site; the
                # `heal` above already uses it. Giving up early is correct
                # here: the wiring is stale, not dangerous-to-leave-one-more-
                # launch, and the next launch tries again. A launch that blocks
                # is the failure this whole module is written to avoid.
                clear_wiring(switcher, timeout=_LAUNCH_LOCK_BUDGET_S, only=dead)
        except Exception:  # noqa: BLE001 — a launch must never fail on the pin
            pass
        return 0

    if set_port is not None:
        # WRITE THE PIN'S OWN SETTING, in the pin's own directory.
        #
        # 0 CLEARS RATHER THAN PERSISTS, and it is not merely out of range:
        # `bind()` reads 0 as "choose one for me", so persisting it would do
        # the OPPOSITE of what a user typing it meant, while looking like it
        # worked. Clearing is the one reading that cannot be mistaken.
        from claude_swap.printer import error

        if set_port and not 0 < set_port <= 65535:
            error(f"Not a port: {set_port}")
            return 1
        # WRITTEN HERE, NOT THROUGH THE PACKAGE. The cert dir is CSWAP's own
        # directory and this is a plain JSON record, so requiring cswap-pin to
        # save it would make the setting unsettable in exactly the case a user
        # is most likely to be fixing something — the same reason `--clear`
        # and `--heal` do their half without the package.
        #
        # READ-MODIFY-WRITE: it is a SETTINGS file, so the next setting to
        # land there must not be erased by the next --set_port.
        try:
            d = _certdir(switcher)
            d.mkdir(parents=True, exist_ok=True)
            path = d / "settings.json"
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raw = {}
            except Exception:  # noqa: BLE001 — absent or garbage: start clean
                raw = {}
            if set_port:
                raw["port"] = int(set_port)
            else:
                raw.pop("port", None)
            switcher._write_json(path, raw)
        except Exception as exc:  # noqa: BLE001
            error(f"Could not save the port: {_safe(exc)}")
            return 1
        return 0

    if get_port:
        # A NUMBER ON STDOUT AND NOTHING ELSE. This is read by `$(cswap pin
        # --port)`, so a prefix, a colour code or a "no pin set" sentence
        # would land INSIDE the caller's variable — turning every consumer
        # back into a parser, which is the thing this flag removes. Silence
        # plus a nonzero exit lets a caller branch without string-matching.
        #
        # BEFORE _impl(), for the same reason as --heal and --clear: the
        # question is most urgent when the package is broken.
        p = serving_port(switcher)
        if p is None:
            return 1
        print(p)
        return 0

    if get_certdir:
        # THE OTHER THING NOBODY COULD ASK FOR, and the one that cost a user's
        # laptop real time. The state directory is not the same path on Darwin
        # as on Linux, and a session diagnosing the pin on a Mac had no way to
        # ask — so it ran, over ssh, on the owner's personal machine:
        #
        #     find ~/Library ~/.local/share -maxdepth 4 -name proxy.json ...
        #
        # Unbounded and hours long, for a string this process already holds.
        # A layout that cannot be ASKED for is a layout every consumer has to
        # SEARCH for, and on someone's laptop that search is the cost.
        #
        # BEFORE _impl() and printing a bare path, for the same two reasons as
        # --get_port: the caller most likely to ask is diagnosing a failure, and
        # the value is read by `$(...)` so a prefix lands in their variable.
        #
        # Unlike --get_port this does NOT probe. The question is "where does
        # this host keep it", which is true whether or not a daemon is up —
        # and a diagnosis of a DEAD pin is exactly when it is asked.
        print(_certdir(switcher))
        return 0

    if heal_only:
        # Deliberately BEFORE _impl(): healing must work when the package is
        # missing or broken, because removing a stale wiring is the half that
        # matters most then. Exit 0 either way — this is meant to be safe to
        # wire into a timer or a shell chain, and a non-zero exit for "nothing
        # was wrong" is noise in both. A CONTRACT, NOT AN OBSERVED CALLER, and
        # the difference is worth the line: comments here (and
        # `_wired_port_of`'s, and the launch hook's) justified budgets with
        # "the status line calls this on a timer". A review read the stale
        # claim and correctly concluded the budgets were wrong for a timer.
        #
        # SO THE BUDGETS HERE STAY THE HUMAN ONES, deliberately: `heal`'s
        # defaults (2.0s probe, 9.0s lock). Hardcoding the launch budget gave
        # the human recovery command 0.5s, and Claude Code holds
        # `.claude.json.lock` routinely during a credential refresh — so the
        # one command whose job is to un-strand you bounced with "the config is
        # locked" where a patient wait would have taken it. `--ensure` is the
        # flag with the launch budgets; that split is the answer.
        changed, msg = heal(switcher)
        print(msg if changed else dimmed(msg))
        return 0

    if clear:
        # Works WITHOUT the package on purpose: ``--clear`` is what a user
        # reaches for precisely when they have uninstalled the pin, and the
        # wiring is cswap's own record (see clear_wiring). Any failure falls
        # back, not just a missing package: "installed but unusable" (a broken
        # cryptography) is the other way a user ends up here, and a traceback
        # is the worst possible outcome for the one command whose job is to
        # work when the pin does not.
        #
        # READ THE RECORD OURSELVES, not through the package. THE SAME
        # clear_pin THE TUI CALLS. A second copy of this logic is how a refusal
        # ends up in one front end and not the other. One decision, one
        # implementation, two renderings.
        ok, msg = clear_pin(switcher)
        if not ok:
            warning(msg)
            return 1
        # PRINT WHAT `clear_pin` DECIDED. This rendered the returned message
        # only when it started with "No " and replaced every other one with
        # "Unpinned the cloud account" — so the stale-receipt success, whose
        # entire value is the PATH it names, reached the TUI (which prints
        # `msg` verbatim) and never the CLI. Two front ends, one decision,
        # opposite output: the divergence the shared `(ok, message)` pair
        # exists to prevent, reintroduced by a startswith.
        #
        # The accent stays on the ordinary unpin, which is the common case and
        # the only one whose wording this layer owns.
        print(
            f"{accent('Unpinned')} the cloud account"
            if msg == "Unpinned the cloud account"
            else msg
        )
        return 0

    pin = _impl()  # raises ClaudeSwitchError with the install hint

    if account is None:
        # Same rule for the read-only path: a malformed pin file is "no pin I
        # can read", not "the package is broken". The TUI badge already answers
        # None in this exact state, so reporting an error here made the two
        # front ends disagree about one file.
        try:
            current = pin.load_pin(switcher.backup_dir)
        except Exception:  # noqa: BLE001
            current = None
        if current:
            print(f"Cloud account (RC/artifacts): {current[0]}")
            _warn_if_bridges_disagree(pin, current)
        else:
            print(dimmed("No cloud account pinned"))
        return 0

    account_num, email, org_uuid = switcher.resolve_account(account)
    # THE SAME set_pin THE TUI CALLS. This branch carried its own copy of the
    # refusal, the rollback and the no-proxy verdict — and the API-key refusal
    # is the divergence that survived in it after the shared pair was added.
    # num is passed, not re-derived: a duplicate email resolves ambiguously
    # and would skip the API-key refusal (see set_pin).
    ok, msg = set_pin(switcher, email, org_uuid, num=account_num)
    if not ok:
        warning(msg)
        if "no proxy is running" in msg:
            print(dimmed("  the daemon log says why: <backup>/pin-proxy/daemon.log"))
        return 1
    print(
        f"{accent('Pinned')} the cloud account (RC/artifacts) to "
        f"Account-{account_num} ({email})"
    )

    # A re-pin takes effect under the live proxy: the pinned account is re-read
    # per request, so nothing has to restart. The one thing it cannot move is a
    # Remote Control session that is ALREADY open — the server fixed its owner
    # at creation, so reconnecting inside it is what mints a new one under the
    # new pin. Name those sessions instead of telling everyone to restart.
    #
    # A NOTE MUST NOT FAIL THE ACTION. The pin is already applied and "Pinned…"
    # has already printed; everything below is advice about which sessions need
    # reconnecting. This was the one call into the optional package in `run()`
    # that no `try` covered, so a raise here — from a peer on its own release
    # schedule — turned a SUCCEEDED pin into: Error: the cloud pin is installed
    # but not usable: … `cswap pin --clear` still works, and removes the wiring
    # … Pinned the cloud account (RC/artifacts) to Account-2 (…) exit 1 Exit 1
    # and advice to `--clear` over a pin that is on disk and working — a user
    # following it destroys it. The TUI's sibling call already guards this
    # (dashboard.py, "a note must not fail the action"); the two front ends
    # disagreeing is the defect this module's header names as its own.
    try:
        open_rc = pin.live_remote_control_sessions()
    except Exception:  # noqa: BLE001 — advice is not the operation
        open_rc = None
    if open_rc:
        which = ", ".join(open_rc[:3])
        if len(open_rc) > 3:
            which += f", +{len(open_rc) - 3} more"
        print(
            dimmed(
                f"Remote Control is open on: {which}. Those stay on the "
                "previous account until you reconnect them "
                "(/rc -> Disconnect this session -> /rc)."
            )
        )
    else:
        print(dimmed("New sessions pick this up."))
    return 0


def _warn_if_bridges_disagree(pin, current) -> None:
    """Say so when the live bridges do not belong to the account we pinned.

    THE STATUS LINE REPORTS THE PIN, WHICH IS WHAT WE WROTE — never what the
    machine has. Measured with three accounts at once:

        the line said       acct1@example.com    pinned, acct 1
        the live bridge was org A                 acct 2
        the login was       org B                 acct 3

    Thirteen live bridges, none on the pinned org, and this command reported
    "pinned" throughout. What ended the silence was the server answering
    `API Error: 500` on a reattach, which cost the user the session.

    NEVER FATAL, and that is the point of the guard rather than tidiness. This
    is the second unguarded call into the optional package on this path; the
    first turned a SUCCEEDED pin into `Error: … not usable` with advice to run
    `--clear`, which would have destroyed it (see
    TestANoteMustNotFailTheAction). `observed_bridge_owners` landed in
    cswap-pin 0.1.85 and the two ship on separate schedules, so a host on an
    older one must lose this extra line and keep the command.

    Only a bridge whose recorded owner DISAGREES is named. An unrecorded owner
    (`None`) is not evidence of anything and stays quiet here — the reader keeps
    that key so a caller can tell unknown from absent, which is a different
    question than this one.
    """
    # Imported here, like `run` does: `printer` is pulled in at call time
    # throughout this module, and a module-level import would be the one
    # difference between this helper and every other renderer in the file.
    from claude_swap.printer import warning

    try:
        owners = pin.observed_bridge_owners()
    except Exception:  # noqa: BLE001 — see the docstring: a raise unpins nothing
        return
    if not isinstance(owners, dict):
        return
    pinned_org = (current[1] if len(current) > 1 else "") or ""
    if not pinned_org:
        return
    other = sorted({o for o in owners.values() if o and o != pinned_org})
    if not other:
        return
    warning(
        "the live Remote Control bridges do not belong to it: "
        f"{len(other)} other organization(s) — {', '.join(other)}. "
        "A reattach against a bridge this login does not own is refused by "
        "the server; the pin is in name only until those sessions restart."
    )
