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

import json
import logging
import os
from types import ModuleType

from claude_swap.exceptions import ClaudeSwitchError, ConfigError

_logger = logging.getLogger("claude-swap")

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
    `_wiring_is_stale`, and because a config that could not be LOCATED is the
    one fact its return value cannot carry: the bool is a claim about every
    path it REACHED. Why a wiring could not be REMOVED is a different record —
    the lock WARNING at the bottom of `clear_wiring`.
    """
    # `stacklevel=2` ATTRIBUTES THE RECORD TO THE CALLER. Without it all three
    # call sites' records are identical in origin — same `funcName`, same
    # `pathname`, same `lineno` — so nothing downstream can tell the per-tick
    # getters from the gated one, and a guard on this split can only key on
    # LEVEL. With it, `record.funcName` is `_wiring_present` / `_wired_ports` /
    # `clear_wiring`.
    #
    # Production output is UNCHANGED: `logging_config` formats
    # "%(asctime)s - %(levelname)s - %(message)s" and never renders funcName,
    # filename or lineno.
    _logger.log(level, "%s could not be resolved: %s", get.__name__, exc, stacklevel=2)


def _install_how() -> str:
    """The install COMMAND for this install method, on its own.

    Split out so there is exactly one place that decides it. A second
    hardcoded `uv tool install ...` survived beside the derived hint and
    diverged from it on a pipx machine — one screen apart, both wrong for
    someone.
    """
    from claude_swap.update_check import _detect_install_method

    return {
        "uv": "uv tool install 'claude-swap[pin]'",
        "pipx": "pipx install 'claude-swap[pin]'",
    }.get(_detect_install_method() or "", "pip install 'claude-swap[pin]'")


def _install_hint() -> str:
    """How to install the extra, in a form that reaches THIS install.

    Not a constant, because `pip install` is wrong for the install method most
    users have. Under a uv tool install, pip puts a second copy in whatever pip
    is on PATH and the extra never reaches the tool's environment — the user
    follows the instruction, it succeeds, and the pin is still missing.
    `cswap upgrade` already solves this; reuse its detector rather than
    re-deriving it.
    """
    return f"The cloud pin requires 'cswap-pin'. Install with: {_install_how()}"


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


# Both display helpers (is_available/pinned_email) are called on every TUI
# RENDER — AccountsPanel.render, AccountCard.render, and twice per
# dashboard._root_entries — not just on the poll, and _live_impl's
# invalidate_caches()+find_spec costs ~0.168ms/call with the extra absent,
# scaling with sys.path length. A TTL well under the TUI's
# poll cadence (POLL_INTERVAL_S = 3.0 in tui/app.py) removes that from every
# render while still noticing a mid-session install: dashboard.refresh_root_menu
# re-renders on every poll tick, so a cache younger than one poll interval is
# stale for at most one render, never for the rest of the session — no
# restart required. Tests must reset this between runs (see conftest.py); it
# is bare module state so nothing else has to plumb a cache handle through.
_LIVE_IMPL_CACHE_TTL_S = 1.0
_live_impl_cache: tuple[float, ModuleType | None] = (float("-inf"), None)


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

    Cached for ``_LIVE_IMPL_CACHE_TTL_S`` (see the module-level comment) so a
    render burst pays for the resolution once, not once per widget.
    """
    import importlib
    import time as _time

    global _live_impl_cache
    cached_at, cached = _live_impl_cache
    now = _time.monotonic()
    if now - cached_at < _LIVE_IMPL_CACHE_TTL_S:
        return cached

    importlib.invalidate_caches()
    try:
        resolved = _impl()
    except Exception:  # noqa: BLE001
        resolved = None
    _live_impl_cache = (now, resolved)
    return resolved


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


# -- launch integration ------------------------------------------------------


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
        #
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
        # the env block from a healthy pin.
        #
        # The probe is bounded well under the launch budget rather than given
        # the default 2s: a black-holed port must not turn a launch-path guard
        # into the stall it was written to avoid.
        #
        # `clear_wiring` logs at most twice per LAUNCH here (its getter WARNING
        # and its lock WARNING), because the gate goes false only when the
        # removal succeeds. At human launch cadence that is negligible, which
        # is why the churn arithmetic lives at the statusline call site.
        try:
            if _wiring_is_stale(switcher, connect_timeout=_LAUNCH_PROBE_S):
                clear_wiring(switcher, timeout=_LAUNCH_LOCK_BUDGET_S)
        except Exception:  # noqa: BLE001
            pass
        return env
    try:
        pinned = pin.ensure_proxy(switcher)
        if pinned:
            port, ca_path = pinned
            return pin.wire_env(env, port, ca_path)
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
            pin.unwire_if_dead(switcher.backup_dir / "pin-proxy")
    except Exception:  # noqa: BLE001
        pass
    return env


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


def clear_wiring(switcher, timeout: float | None = None) -> bool:
    """Remove a pin wiring from the global config. True when it removed one.

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
    from claude_swap.claude_locks import proper_lockfile
    from claude_swap.paths import (
        get_default_global_config_path,
        get_global_config_path,
    )

    # BOTH configs, because the writing side resolves the same way this does:
    # `CLAUDE_CONFIG_DIR` is set in the *child's* env dict, not the process's,
    # so a `cswap run` from a normal terminal wires ~/.claude.json while one
    # from inside a session terminal wires that session's copy. Clearing only
    # the resolved path leaves the other wired, and `cswap pin --clear` then
    # prints "No cloud account pinned" over a config that still names a dead
    # port — the exact stranding this function exists to prevent. The two
    # paths diverge as soon as CLAUDE_CONFIG_DIR is set.
    #
    # EACH GETTER CAN RAISE (see the same guard on `_wired_ports` and
    # `_wiring_present`): `get_default_global_config_path` calls `Path.home()`,
    # which raises `RuntimeError` with no HOME and no `/etc/passwd` entry. A
    # config this call cannot even locate has nothing to clear there — that
    # is a fact about ONE config, not a reason to abandon the other.
    #
    # LOGGED, not just skipped: a config that could not be RESOLVED and one
    # that resolved with nothing wired both leave this loop silently short a
    # path, and `clear_wiring`'s bool is a claim about every path it reached
    # — not a claim that every path was reachable. Without a record, "the
    # default profile was never attempted because HOME could not be found"
    # and "the default profile was attempted and had nothing wired" are the
    # same silence from the outside.
    paths = []
    for get in (get_global_config_path, get_default_global_config_path):
        try:
            path = get()
        except Exception as exc:  # noqa: BLE001 — unresolvable: no opinion
            # WARNING HERE ONLY. `heal` reaches `clear_wiring` through
            # `_wiring_is_stale`, which goes false ONCE THE REMOVAL SUCCEEDS,
            # so this logs once and goes quiet. The two getters `heal` calls
            # UNCONDITIONALLY stay at DEBUG (see `_log_unresolvable`).
            #
            # THIS RECORD DOES NOT EXPLAIN AN UNREMOVABLE WIRING, and must not
            # be read as if it did. On the flagship shape — read-only config
            # dir, HOME resolvable — nothing raises here and it never fires;
            # what fires is `heal`'s own "the config is locked" message. Make
            # `Path.home()` raise too and this names
            # `get_default_global_config_path` while the STUCK config is the
            # one the other getter resolved fine. Put the wiring in the raising
            # getter's config and `_wiring_present` cannot see it either, so
            # `heal` answers "Nothing to heal" and never reaches this function.
            #
            # What names an unremovable wiring is the lock-failure WARNING at
            # the bottom of this function. This record's job is the narrower
            # one it can do: a config that could not be LOCATED is missing from
            # `paths`, and `clear_wiring`'s bool is a claim about every path it
            # REACHED.
            _log_unresolvable(get, exc, logging.WARNING)
            continue
        if path not in paths:
            paths.append(path)

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
        from claude_swap.claude_locks import DEFAULT_TIMEOUT_S

        timeout = DEFAULT_TIMEOUT_S
    deadline = _time.monotonic() + timeout
    changed = False
    for i, path in enumerate(paths):
        left = deadline - _time.monotonic()
        if left <= 0:
            continue  # budget spent; the next launch heals what is left
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
            # config is locked)" every tick with zero records at any level.
            # The getter WARNING above does not fire on that shape.
            #
            # KEPT AT WARNING FOR BOTH REACHABLE KINDS. `PermissionError` and
            # `ClaudeCodeLockTimeout` both land here, and the type does not
            # separate transient from permanent: a live Claude Code credential
            # refresh raises the timeout, and so does an orphaned lock dir
            # inside a directory this process cannot write, which never
            # resolves. Splitting on type would silence the stuck machine this
            # WARNING exists for.
            #
            # The lock dir's mtime age WOULD separate them (`proper_lockfile`
            # already reads it against `CONFIG_STALENESS_S`), but the transient
            # case is self-limiting — the competitor lets go and the next free
            # tick unwires — so it costs ~2 lines once, against a permanent
            # case that repeats forever. Not worth the arithmetic.
            _logger.warning("%s could not be unwired: %s", path, exc)
            continue
    return changed


def _config_lock_is_free(budget: float) -> bool:
    """Can the config lock be taken within ``budget`` seconds?

    A probe, not a hold — the caller re-locks immediately after. That race is
    deliberate: losing it costs one skipped unwire (the next launch heals it),
    while the alternative is the launch itself waiting on the package's own
    5-second lock timeout, which it has no way to shorten.
    """
    from claude_swap.claude_locks import proper_lockfile
    from claude_swap.paths import get_global_config_path

    path = get_global_config_path()
    try:
        with proper_lockfile(path.parent / (path.name + ".lock"), timeout=budget):
            return True
    except Exception:  # noqa: BLE001
        return False


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
    try:
        _impl().apply_pin(switcher, *(before or (None, None)))
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
#
# The pin writes proxy vars into `.claude.json`'s `env` block and needs a
# receipt saying WHICH keys are its own, so unwiring restores exactly what it
# displaced and touches nothing else.
#
# THE ENV BLOCK CANNOT MOVE. Claude Code reads it out of `.claude.json` at
# boot; that file IS the interface. THE RECEIPT CAN, and should: it is
# bookkeeping only cswap reads, and `.claude.json` is the user's file — every
# key we leave in it is one more thing a human editing that file can trip
# over, and two of them (`_cswapPinWiredKeys`, `_cswapPinWiredKeysSaved`) are
# opaque unless you know this code.
#
# READ BOTH, WRITE NEW. The sidecar is authoritative when present; the config
# is still read for the copy every existing install has. A pin OLDER than this
# change keeps writing the config key, and it must keep working — so this is
# not a migration with a cutover, it is two readers and one writer, and the
# old location stays readable indefinitely.
_LEDGER_FILE = "pin-wiring.json"


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

    from claude_swap.paths import get_backup_root

    key = hashlib.sha256(str(config_path).encode("utf-8")).hexdigest()[:16]
    return get_backup_root() / "pin-wiring" / f"{key}.json"


def _read_ledger(config_path) -> dict | None:
    """The sidecar receipt, or None when there is none to read."""
    try:
        raw = json.loads(_ledger_path(config_path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — absent/unreadable is not "wired"
        return None
    return raw if isinstance(raw, dict) else None


def _clear_ledger(config_path) -> None:
    """Record "not wired" in the sidecar. Best-effort, never raises.

    WRITES AN EMPTY MARKER rather than deleting the file. `_wire_mark_of`
    treats a sidecar that says "not wired" as an ANSWER and stops there; a
    DELETED sidecar is a miss, and the read falls through to the config — so
    unlinking would let an old config key that a failed earlier write left
    behind resurrect a wiring this call just removed.
    """
    path = None
    try:
        path = _ledger_path(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({_WIRE_MARK: []}), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — the config write is what matters
        if path is not None:
            try:
                path.with_name(f"{path.name}.{os.getpid()}.tmp").unlink()
            except OSError:
                pass


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
        if side is not None:
            ours = side.get(_WIRE_MARK)
            if isinstance(ours, list) and ours:
                return ours
            # A sidecar that exists and says "not wired" is an ANSWER, not a
            # miss: `--clear` writes it. Falling through to the config here
            # would resurrect a receipt the clear deliberately emptied.
            if _WIRE_MARK in side:
                return None
    if not isinstance(raw, dict):
        return None
    ours = raw.get(_WIRE_MARK)
    return ours if isinstance(ours, list) and ours else None


def _saved_of(raw: object, config_path=None) -> dict:
    """What the wiring displaced, from wherever the receipt lives.

    Same read-both rule as :func:`_wire_mark_of`, and it must stay paired with
    it: reading the marker from the sidecar and the displaced values from the
    config would restore one wiring's values over another's keys.
    """
    if config_path is not None:
        side = _read_ledger(config_path)
        if side is not None and _WIRE_MARK in side:
            saved = side.get(f"{_WIRE_MARK}Saved")
            return dict(saved) if isinstance(saved, dict) else {}
    if not isinstance(raw, dict):
        return {}
    saved = raw.get(f"{_WIRE_MARK}Saved")
    return dict(saved) if isinstance(saved, dict) else {}


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
    # ONE TRAVERSAL, in `wired_config_paths`. This used to walk the configs
    # itself, and `purge` needed the same walk to name the survivor — two
    # copies of "which configs are wired" is two things to keep in step, and
    # the one that drifted was the one a user reads after their only other
    # recourse has been deleted.
    #
    # The getter-raises guard, the de-dup and the unreadable-is-not-wired rule
    # all live there now; see that function for why `heal` cannot afford a
    # raise to escape.
    return bool(wired_config_paths(_switcher))


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
    from claude_swap.paths import (
        get_default_global_config_path,
        get_global_config_path,
    )

    wired, seen = [], set()
    for get in (get_global_config_path, get_default_global_config_path):
        # The getter itself can raise; see `_wiring_present` above for why
        # `heal` cannot afford that to escape.
        try:
            path = get()
        except Exception as exc:  # noqa: BLE001 — unresolvable: no opinion
            _log_unresolvable(get, exc)
            continue
        if path in seen:
            continue
        seen.add(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — unreadable/absent is not "wired"
            continue
        if _wire_mark_of(raw, path) is not None:
            wired.append(path)
    return wired


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
    _clear_ledger(path)
    return True


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
    try:
        impl = _impl()
        impl.apply_pin(switcher, None, None)
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
    cleared = clear_wiring(switcher)
    still_pinned = _pinned_email_now(switcher) is not None
    still_wired = _wiring_present(switcher)
    if still_pinned or still_wired:
        what = " and ".join(
            w for w, on in (("the pin", still_pinned), ("the wiring", still_wired)) if on
        )
        return False, f"Could not remove {what} — re-run once it frees up"
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
        started = _impl().apply_pin(switcher, email, org_uuid)
    except Exception as exc:  # noqa: BLE001 — a traceback tells a user nothing
        rolled = _restore_pin(switcher, before)
        return False, (
            f"Could not pin the cloud account: {_safe(exc)} — "
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
            "so nothing is pinned yet — " + _rollback_tail(rolled, before, email)
        )
    return True, f"Pinned the cloud account (RC/artifacts) to {email}"


def _wiring_is_stale(_switcher, connect_timeout: float = 2.0) -> bool:
    """Should this wiring be removed? Present AND not serving.

    THE VERDICT LIVES HERE, NOT AT EACH CALL SITE — the rule this file already
    states for the pin record, applied to the wiring. It was not, and the two
    places that forgot the serving half both tore down a working pin:

      * ``heal`` had the guard.
      * ``wire_launch_env`` did not. With ``_impl()`` raising for a reason
        unrelated to the daemon (a broken ``cryptography`` — precisely the case
        ``_impl`` re-raises separately), one ``cswap run`` unwires a pin whose
        port is answering, and every session on the box loses it.

    ``connect_timeout`` exists because the launch path has a sub-second budget
    and a black-holed port would otherwise blow it on the probe alone.

    ``_switcher`` is unused (see :func:`_wiring_present`) but kept, and
    underscore-prefixed, so this predicate stays call-compatible with its
    sibling and every call site can keep passing the switcher it already has
    on hand without checking which predicate needs it.
    """
    # THIS GUARD DOES A SECOND JOB the comment below (added when the
    # per-config verdict became machine-wide) does not mention: it is also
    # the ONLY thing left checking the `_cswapPinWiredKeys` MARKER before a
    # port is treated as cswap's to condemn. The short-circuit this replaced
    # read only the session config's own port (a now-deleted per-config
    # helper — see Task 3 of this file's history) and so incidentally never
    # reached the serving probe for a config cswap never wired — the marker
    # was never checked explicitly, but the narrow per-config read meant it
    # didn't have to be. `_wired_ports()` below reads BOTH configs' ports
    # with NO marker check at all, so without this line a foreign
    # `CSWAP_PIN_PORT` (no marker, e.g. a future `cswap-pin` release that
    # stops writing it, or an unrelated var of the same name) sitting in
    # either config and pointing at a dead port makes `_wiring_is_stale`
    # True with nothing of cswap's actually wired.
    #
    # Without this line: `_wiring_present=False`, `_wired_ports=[<dead>]`,
    # `_wiring_is_stale=True`, and `heal()` reports "Removed a cloud pin
    # wiring…" over a byte-for-byte unchanged config — a false removal claim
    # in the machine-readable channel the status line polls. Nothing is ever
    # mutated (`_clear_wiring_locked` refuses a markerless file); the damage
    # is entirely in the VERDICT this guard keeps honest.
    if not _wiring_present(_switcher):
        return False
    # "I CANNOT TELL" IS NOT "IT IS DEAD". `_wiring_present` keys on the
    # marker; the serving probe reads CSWAP_PIN_PORT. A config carrying the
    # marker and no port satisfied both "wired" and "not serving" at once, so
    # the launch path tore it down — against a proxy that may be perfectly
    # live.
    #
    # Today's writer always emits the port, so this is not reachable through
    # it. But the seam's stated threat model is that the package is a PEER on
    # an independent release schedule, and refusing to trust its return value
    # while trusting its file FORMAT with the destructive operation is the same
    # inference this module keeps being burned by.
    #
    # MACHINE-WIDE, not per-config: this guards a WHOLE-MACHINE action
    # (`clear_wiring` clears every wired config), so "I cannot tell" has to
    # mean nothing ON THE MACHINE names a readable port — not merely that
    # THIS config alone does not. The shipped deployment shape makes the
    # narrower reading the COMMON case, not a corner one: `cswap run` wires
    # ~/.claude.json and launches a child whose OWN config is seeded with no
    # wiring at all, and the status line hook inside that child is what calls
    # `heal` on a timer. So the process that heals is normally the one whose
    # own config has no port to name — and a dead port sitting in the OTHER
    # config must still be reachable. With the own config unwired and
    # ~/.claude.json wired to a dead port, a per-config read sees None, returns
    # False, and `heal()` answers "Nothing to heal" over a dead port.
    if not _wired_ports():
        return False
    return not _wired_port_is_serving(_switcher, connect_timeout=connect_timeout)


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
    --heal` — called from the status line on a timer, where `heal`
    documents "never raises". Treating it as "no opinion" here, at the
    source, means every downstream consumer inherits the fix for free.
    """
    import json as _json

    try:
        raw = _json.loads(path.read_text(encoding="utf-8"))
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


def _wired_ports() -> list[int]:
    """Every pin port the configs name, in read order. Unreadable ones are
    absent rather than zero — "no opinion" and "port 0" are different facts.

    For "is ANYTHING wired at all" questions (``_wiring_present``,
    ``clear_wiring``, the every-config-must-serve probe below) where both
    configs' opinions genuinely apply at once.
    """
    from claude_swap.paths import (
        get_default_global_config_path,
        get_global_config_path,
    )

    ports, seen = [], set()
    for get in (get_global_config_path, get_default_global_config_path):
        # THE GETTER ITSELF CAN RAISE. `get()` is not just "resolve a path and
        # a set membership test": `get_default_global_config_path` calls
        # `Path.home()`, which raises RuntimeError when HOME is unset and the
        # uid has no /etc/passwd entry (the standard rootless-container
        # shape). `heal`'s docstring promises "never raises" because the
        # status line calls it on a timer, and this function sits on the path
        # from `heal` through `_wired_port_is_serving` with no guard above it
        # — an unguarded raise here reaches the status line's caller directly,
        # so `pin.heal(sw)` raises RuntimeError instead of returning
        # ``(False, 'Could not heal…')``.
        try:
            path = get()
        except Exception as exc:  # noqa: BLE001 — unreadable/unresolvable: no opinion
            _log_unresolvable(get, exc)
            continue
        if path in seen:
            continue
        seen.add(path)
        port = _port_of_config(path)
        if port:
            ports.append(port)
    return ports


def _wired_port_is_serving(_switcher, connect_timeout: float = 2.0) -> bool:
    """Is the port the CONFIG names actually answering?

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
    import socket

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
    for port in ports:
        sock = socket.socket()
        sock.settimeout(connect_timeout)
        try:
            sock.connect(("127.0.0.1", port))
        except OSError:
            return False  # a config names a port nothing serves
        finally:
            sock.close()
    return bool(ports)


def heal(switcher) -> tuple[bool, str]:
    """Make the pin serving again, or make it harmless. ``(changed, message)``.

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

    Never raises: this is called from the status line every few seconds, and a
    health check that can break the prompt is worse than the fault it reports.
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
            # RE-READ THE TRUE AS WELL AS THE FALSE. The branch below already
            # refuses to infer an outage from a False; trusting a True was the
            # same mistake pointing the other way, and this function's whole
            # thesis is that a verdict comes from the state, not from a call.
            #
            # It matters because the package is a PEER on its own release
            # schedule (see _impl): the seam cannot promise what a future
            # version returns. An impl that returns True while binding
            # nothing gives `heal() -> (True, "Restored the cloud pin")` with
            # the wired port not serving, so the status line shows healthy
            # while every session dials a dead port.
            if impl.heal(switcher.backup_dir) and _wired_port_is_serving(switcher):
                return True, "Restored the cloud pin"
        except Exception:  # noqa: BLE001 — fall through to the safe outcome
            pass
        # The restart may have succeeded while returning False (it also uses
        # False for "already serving"). Re-READ rather than infer: unwiring a
        # pin that just came back is the same damage as unwiring a live one.
        if _wired_port_is_serving(switcher):
            return False, "Nothing to heal"
    elif _wired_port_is_serving(switcher):
        # No package, so nothing can restart OR recycle — but a serving pin is
        # still a working one, and removing its wiring would unpin a healthy
        # session. The guard has to survive the package being absent, which is
        # exactly when a user can least afford a wrong answer.
        #
        # The port the WIRING names is the right question, not any state file:
        # `_spawn_daemon` unlinks proxy.json as its first act, so a missing
        # record is not proof of death while the original daemon still serves.
        return False, "Nothing to heal"
    # No package, or the restart failed. Either way the wiring must not outlive
    # the daemon it points at. clear_wiring works WITHOUT the package on
    # purpose — the wiring is cswap's own record, and the case where the extra
    # is broken is exactly when a user cannot afford to be stranded.
    #
    # AND SAY WHICH OF THE TWO HAPPENED. `present and clear_wiring(...)`
    # collapsed "there was nothing to remove" into "I could not remove it", and
    # fell through to the healthy verdict for both. The second is reachable and
    # routine: the budget here is 0.5s and Claude Code holds the config lock
    # during a credential refresh. With the lock held, a wiring present and
    # the port dead, `heal` answers (False, "Nothing to heal") over an outage
    # in progress and the wiring survives.
    #
    # That is this file's signature defect, in the channel that matters most:
    # the status line calls `heal` on a timer, so during the exact failure it
    # exists to report, the user's only signal said everything was fine.
    #
    # RE-READ AFTER CLEAR_WIRING, exactly as clear_pin already does — its
    # bool is True when ANY of the two configs changed, not when BOTH did.
    # With the session config's lock held and the default config free,
    # clear_wiring clears the default and returns True for that one change,
    # so `heal` reports "Removed a cloud pin wiring" while the session config
    # still names the dead port.
    #
    # THE SAME QUESTION `_wiring_is_stale` ASKS, not `_wiring_present` alone.
    # `_wiring_present` keys on the marker only, so a config carrying the
    # marker with no readable CSWAP_PIN_PORT satisfied it and got torn down
    # here — the exact shape `_wiring_is_stale`'s own guard (see its
    # docstring) declares must not be read as "the proxy is dead". `heal` is
    # the worse of the two call sites to leave unguarded: the status line
    # calls it on a timer, unattended, while the launch path runs once.
    try:
        if _wiring_is_stale(switcher):
            clear_wiring(switcher, timeout=_LAUNCH_LOCK_BUDGET_S)
            if not _wiring_present(switcher):
                return True, (
                    "Removed a cloud pin wiring whose proxy was gone — "
                    "sessions fall back to the proxy they had before the pin"
                )
            return False, (
                "A cloud pin wiring points at a proxy that is gone, and it "
                "could not be removed (the config is locked) — re-run "
                "`cswap pin --heal`"
            )
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not heal the cloud pin ({_safe(exc)})"
    return False, "Nothing to heal"


def serving_port(switcher) -> int | None:
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
    import json as _json
    import socket
    from pathlib import Path

    record = Path(switcher.backup_dir) / "pin-proxy" / "proxy.json"
    try:
        port = int(_json.loads(record.read_text(encoding="utf-8"))["port"])
    except Exception:  # noqa: BLE001 — absent/unreadable/malformed: no opinion
        return None
    if not 0 < port <= 65535:
        return None
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2.0):
            return port
    except OSError:
        return None


def configured_port(switcher) -> int | None:
    """The port the user asked the pin to serve on, or None.

    Delegated to the package, which OWNS the setting — the seam must not grow
    a second reader of a file cswap-pin writes, because two readers of one
    fact is how `--clear` stopped converging once already. Falls back to the
    environment alone when the package is absent, so the answer is still the
    user's own export rather than nothing.
    """
    # THE SAME ORDER THE PACKAGE USES, and read here rather than delegated:
    # the setting lives in CSWAP's own directory and is a plain JSON record,
    # so making the answer depend on an optional package would leave it
    # unreadable in the case a user is most likely to be diagnosing.
    #
    #   1. the ENVIRONMENT — what the user typed for THIS shell
    #   2. the settings file — what they saved once
    #   3. nothing — an ephemeral port
    #
    # RANGE-CHECKED, and 0 is the one that matters: bind() reads it as
    # "choose one for me", so treating a configured 0 as a request would do
    # the opposite of what it says.
    saved = None
    try:
        raw = json.loads(
            (_certdir(switcher) / "settings.json").read_text(encoding="utf-8")
        )
        saved = raw.get("port") if isinstance(raw, dict) else None
    except Exception:  # noqa: BLE001 — absent/unreadable/malformed: no opinion
        pass
    # THE ENV VAR HAS TWO AUTHORS, and only one is the user. cswap-pin writes
    # `CSWAP_PIN_PORT` into `.claude.json`'s env block as its self-loop marker,
    # and Claude Code applies that block at boot — so every process inside a
    # pinned session inherits it, including this one. Reading it back as a
    # SETTING makes the pin's own address look like something the user asked
    # for. `CSWAP_PIN_WIRED` is written beside it and nowhere else, so it
    # identifies our value; an rc export has no companion. Same rule, same
    # reasoning, as the package's own `_env_port`.
    env_value = None if os.environ.get("CSWAP_PIN_WIRED") else os.environ.get(
        "CSWAP_PIN_PORT"
    )
    # THE ENVIRONMENT ANSWERS OR IT DOES NOT — it never falls through to the
    # file. `CSWAP_PIN_PORT=0` means "let the kernel choose", the same thing
    # `--set_port 0` means; falling through made one word mean two things, and
    # an rc export could not force a dynamic port on a machine that had ever
    # been given a fixed one. A TYPO still falls through: it is not an
    # instruction, and the saved setting beats nothing.
    if env_value is not None:
        try:
            port = int(env_value)
        except (TypeError, ValueError):
            port = None
        if port == 0:
            return None
        if port is not None and 0 < port <= 65535:
            return port
    try:
        port = int(saved)
    except (TypeError, ValueError):
        return None
    return port if 0 < port <= 65535 else None


def _certdir(switcher):
    """Where the pin keeps its own files. One definition, so a layout change
    is one edit rather than a grep."""
    from pathlib import Path

    return Path(switcher.backup_dir) / "pin-proxy"


def run(
    switcher,
    account: str | None,
    clear: bool = False,
    heal_only: bool = False,
    get_port: bool = False,
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
            # NOTHING WIRED AND NOTHING RECORDED IS THE COMMON CASE. Healing
            # unconditionally would spend a config read, two path resolutions
            # and a socket probe per launch for a user who has never pinned —
            # and the status line already calls `heal` on a timer, so on a
            # machine with both this would be the second caller per tick.
            if not _wiring_present(switcher) and _pinned_email_now(switcher) is None:
                return 0
            heal(switcher)
            # RE-READ, DO NOT TRUST THE RETURN. This is disaster path D from
            # the lmd42 outage, one level in: an old cswap REJECTED `--heal`
            # with exit 2, the call was made, the rejection went unread, and
            # the machine stayed stranded for days. `pin-ensure` answers that
            # by re-reading the config rather than believing the command, and
            # the same exposure exists here — `heal` calls into `cswap_pin`,
            # a PEER on its own release schedule, so a version that reports
            # success while binding nothing gives a launch hook that did its
            # job and a session that dials a dead port anyway.
            #
            # The wiring is CSWAP'S OWN record, so removing it needs no
            # package at all: unpinned is a working session, wired-to-a-dead-
            # port is not.
            if _wiring_is_stale(switcher):
                clear_wiring(switcher)
        except Exception:  # noqa: BLE001 — a launch must never fail on the pin
            pass
        return 0

    if set_port is not None:
        # WRITE THE PIN'S OWN SETTING, in the pin's own directory. This number
        # used to live in `~/.claude.json`'s env block — the one entry there
        # Claude Code does not read — so a user who wanted a fixed port had
        # nowhere to say so and we were storing our config in another
        # program's exclusive file.
        #
        # 0 CLEARS rather than persists. It is not merely out of range:
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

    if heal_only:
        # Deliberately BEFORE _impl(): healing must work when the package is
        # missing or broken, because removing a stale wiring is the half that
        # matters most then. Exit 0 either way — the status line calls this on
        # a timer and a non-zero exit for "nothing was wrong" is noise.
        changed, msg = heal(switcher)
        print(msg if changed else dimmed(msg))
        return 0

    if clear:
        # Works WITHOUT the package on purpose: ``--clear`` is what a user
        # reaches for precisely when they have uninstalled the pin, and the
        # wiring is cswap's own record (see clear_wiring).
        #
        # Any failure falls back, not just a missing package: "installed but
        # unusable" (a broken cryptography) is the other way a user ends up
        # here, and a traceback is the worst possible outcome for the one
        # command whose job is to work when the pin does not.
        # READ THE RECORD OURSELVES, not through the package.
        #
        # THE SAME clear_pin THE TUI CALLS. A second copy of this logic is how
        # a refusal ends up in one front end and not the other. One decision,
        # one implementation, two renderings.
        ok, msg = clear_pin(switcher)
        if not ok:
            warning(msg)
            return 1
        print(msg if msg.startswith("No ") else f"{accent('Unpinned')} the cloud account")
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
    # A NOTE MUST NOT FAIL THE ACTION. The pin is already applied and
    # "Pinned…" has already printed; everything below is advice about which
    # sessions need reconnecting. This was the one call into the optional
    # package in `run()` that no `try` covered, so a raise here — from a peer
    # on its own release schedule — turned a SUCCEEDED pin into:
    #
    #     Error: the cloud pin is installed but not usable: …
    #       `cswap pin --clear` still works, and removes the wiring …
    #     Pinned the cloud account (RC/artifacts) to Account-2 (…)
    #     exit 1
    #
    # Exit 1 and advice to `--clear` over a pin that is on disk and working —
    # a user following it destroys it. The TUI's sibling call already guards
    # this (dashboard.py, "a note must not fail the action"); the two front
    # ends disagreeing is the defect this module's header names as its own.
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
