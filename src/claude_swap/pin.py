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

# `clear_wiring` stays here rather than in the optional package: the wiring it
# removes strands every new session on a dead port, and the case it exists for
# is that package being broken or gone.


# The wiring detectors stay here with the remover they drive. In the package
# they answered "nothing is wired" whenever the extra was absent, which is
# exactly when `clear_wiring` must still run. None of it is pin policy: path
# arithmetic on cswap's backup dir, a read of cswap's own config, a loopback
# connect.


def _certdir(switcher):
    """Where the pin keeps its own files. The ONLY spelling of this path.

    `test_the_certdir_literal_appears_exactly_once` enforces that, because a
    prose claim about a grep is a claim nothing checks: this docstring made
    one twice while other call sites still built the path themselves."""
    from pathlib import Path

    return Path(switcher.backup_dir) / "pin-proxy"


def _port_answers(port: int, connect_timeout: float) -> bool:
    """Does a loopback connect to ``port`` succeed? The one probe, once.

    Every caller needs a different SHAPE of the answer -- the machine-wide AND
    (:func:`_wired_port_is_serving`), one per config
    (:func:`_dead_wired_configs`), the port itself (:func:`serving_port`) --
    and a second copy would be a second place for a timeout or an exception
    class to drift.
    """
    import socket

    # `socket.socket()` itself raises OSError on fd exhaustion, so it is
    # inside the try; `sock` is bound first so `finally` cannot raise
    # UnboundLocalError past the handler. This must never raise -- `heal`
    # calls it unguarded.
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
        # Only a port THIS tool wired: a ``CSWAP_PIN_PORT`` with no marker
        # was put there by something else and its liveness says nothing about
        # ours. Through ``_wire_mark_of`` so "names a port" and "is wired"
        # stay the same question everywhere.
        if _wire_mark_of(raw, path) is None:
            return None
        env = raw.get("env") or {}
        port = int(env.get("CSWAP_PIN_PORT") or 0)
    except Exception:  # noqa: BLE001 — unreadable/unwired: no opinion
        return None
    return port if 0 < port <= 65535 else None


def _dead_wired_configs(_switcher, connect_timeout: float = 2.0) -> list:
    """Every wired config whose OWN port is not answering -- and no more.

    THE VERDICT IS MACHINE-WIDE; THE ACT MUST NOT BE. `_wired_port_is_serving`
    is AND over every wired config, so one dead config makes the whole machine
    "not serving" -- correct, because a live session config must not mask a
    dead default one. But `clear_wiring` is unconditional over every wired
    config, and composing the two unwired the OTHER config's live pin:

        session cfg -> 42967 (LIVE)   default cfg -> 39967 (DEAD)
        stale verdict : True      ->  clear_wiring strips BOTH

    This list is also the staleness verdict, one bool wide: "should any wiring
    be removed" is exactly "is any wired config's own port dead".

    Whether any of it is cswap's to condemn at all is asked by
    :func:`_port_of_config`, once per config, and not again here.
    """
    # Both guards are enforced one scope down, in `_port_of_config`: an
    # unmarked foreign port must not make this list non-empty, and "I cannot
    # read the port" is not "the port is dead". Either would have the launch
    # path tear down a wiring whose proxy may be live.
    return [
        path
        for path in _each_config()
        if (port := _port_of_config(path)) and not _port_answers(port, connect_timeout)
    ]


def clear_wiring(switcher, timeout: float | None = None, only=None,
                 unsplice=None) -> bool:
    """Remove a pin wiring from the global config. True when it removed one.

    ``only`` narrows it to the given config paths. The default -- every wired
    config -- is what ``cswap pin --clear`` means and must not change: leaving
    one config wired is the stranding this function exists to prevent.
    ``heal`` is the caller that needs less, because its trigger is per-config
    (see :func:`_dead_wired_configs`) while its remedy was not.

    The pin writes its proxy address into ``.claude.json``'s env block and
    records which keys it wrote in ``_cswapPinWiredKeys``; this reads that
    marker and puts the file back. Only keys the pin recorded are touched and
    anything it displaced is restored, so a proxy the user set beforehand comes
    back rather than being lost with ours. It touches no proxy, no daemon and
    no credential.

    It has to be here rather than in the optional package because the failure
    it prevents is that package being GONE: the env block is applied at boot,
    so a wiring naming a dead port makes every hand-launched ``claude`` dial it
    forever, and only code that does not need the package can remove it.
    """
    from claude_swap.claude_locks import proper_lockfile

    # Both configs: `CLAUDE_CONFIG_DIR` is set in the child's env, so a run
    # from a normal terminal wires ~/.claude.json while one from a session
    # terminal wires that session's copy. WARNING here only -- the return bool
    # is a claim about every path REACHED, never that every path was
    # reachable, so a config that could not be located has to leave a record.
    paths = list(_each_config(logging.WARNING))
    if only is not None:
        # BY RESOLVED PATH, not by identity: the caller got its list from
        # `_each_config` too, but a getter that resolves through a symlink or
        # a different Path flavour would silently filter everything out — and
        # an empty `paths` here is a clear that removes nothing while
        # reporting the same False as "there was nothing to remove".
        wanted = {str(p) for p in only}
        paths = [p for p in paths if str(p) in wanted]

    # One lock per path: the shared config lock derives its directory from
    # get_global_config_path(), so a single lock around the loop leaves the
    # other file unprotected. ``timeout`` is a TOTAL, not per-file -- passing
    # it to each acquisition makes the worst case a multiple of the configs.
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
        # Every path is attempted even with the budget gone: a zero share is
        # not a skip, because `proper_lockfile` tries `os.mkdir` before it
        # checks its deadline, so a free lock is still taken instantly.
        left = max(0.0, deadline - _time.monotonic())
        # Fair share of what remains, not all of it: one contended config
        # would otherwise consume the whole budget and the next path -- whose
        # lock may be free -- is never tried. Each share is carved out of
        # `left`, so the running total can never exceed `timeout`.
        share = left / (len(paths) - i)
        try:
            with proper_lockfile(
                path.parent / (path.name + ".lock"), timeout=share
            ):
                if _clear_wiring_locked(switcher, path, unsplice):
                    changed = True
        except Exception as exc:  # noqa: BLE001
            # A lock we cannot take skips THIS file, never the other one.
            # Say which and why: this is the only record naming what stopped
            # the unwire. WARNING for both reachable kinds -- the exception
            # type does not separate transient from permanent.
            _logger.warning("%s could not be unwired: %s", path, exc)
            continue
    return changed


def _config_address(oauth) -> str:
    """``oauthAccount``'s ``emailAddress``, casefolded. "" when not a string.

    `.claude.json` is a file a human edits, and all THREE readers of this
    field casefold it: two on the clear path, whose contract is "never
    raises", and `_config_already_names` on the rollback, where a raise is
    swallowed into a False that reports a clean rollback as a failure.
    """
    value = oauth.get("emailAddress")
    return value.casefold() if isinstance(value, str) else ""


def _config_names_the_pin(switcher, current: dict, pinned) -> bool:
    """Does this config's ``oauthAccount`` name the pinned account?

    THE RECORD AND THE CONFIG SPEAK DIFFERENT VOCABULARIES for the org uuid,
    so never compare across them. The record's ``pinnedOrganizationUuid`` is
    the ROSTER row's -- `account_is_pinned` reads it that way -- while the
    splice writes the account's OWN ``oauthAccount``, which is what Claude
    Code compares a bridge owner against and which a backup config may carry
    with no org key at all.

    So ask the WRITER. Whatever `identity_for_config` answers for the pinned
    account is what was spliced, and it carries an ``accountUuid`` -- a
    stronger key than the composite, and one the record does not hold.

    THE COMPOSITE REMAINS THE FALLBACK, because an address alone is not an
    account: the documented personal/org pattern puts one address in two
    slots, and an email-only test cannot tell a splice from a genuine /login
    into a same-address sibling.

    The address gate below is deliberately LOOSER than everything under it:
    it casefolds, because Claude Code round-trips ``emailAddress`` through
    its own login, while `_slot_for` and the row count match the roster
    exactly, as `_resolve_account_identifier` does.
    """
    if _config_address(current) != (pinned[0] or "").casefold():
        return False
    # THE SLOT, NEVER THE ADDRESS ALONE: given only an email,
    # `identity_for_config` falls to `_resolve_account_identifier`, which
    # RAISES on an address naming two slots. Every other caller here pairs
    # the two for that reason. No try needed -- `identity_for_config` wraps
    # its whole body in `except Exception: return None`.
    slot = _slot_for(switcher, pinned[0], pinned[1])
    mine = identity_for_config(switcher, email=pinned[0], num=slot) or {}
    uuid = mine.get("accountUuid")
    if uuid:
        return current.get("accountUuid") == uuid
    if slot is None:
        # NO SLOT PLUS AN AMBIGUOUS ADDRESS: the composite cannot arbitrate.
        # A record whose org names no roster row leaves both rows at this
        # address equally plausible, and an org-less sibling config satisfies
        # email-and-org while being a genuine /login. A stale name costs a
        # bridge; a wrong rewrite swaps the identity -- so decline.
        #
        # COUNTED, not inferred from a raise. `_resolve_account_identifier`
        # raises on this shape, but it raises on an unreadable `sequence.json`
        # too, and turning on the exception declines on a roster we merely
        # could not read -- narrowing the clear on the path that exists for
        # when things are broken. An unreadable roster is not ambiguity.
        try:
            rows = (switcher._get_sequence_data() or {}).get("accounts") or {}
            same = sum(1 for r in rows.values()
                       if isinstance(r, dict) and r.get("email") == pinned[0])
        except Exception:  # noqa: BLE001 — unreadable is not ambiguous
            same = 0
        if same > 1:
            # THE ONLY RECORD THAT THIS HAPPENED. `clear_pin` decides on the
            # record and the env keys, neither of which sees the splice, so
            # it reports success over a config that still names the ex-pin.
            _logger.warning(
                "the pin record names an address held by %d accounts and "
                "cannot say which; leaving oauthAccount alone", same)
            return False
    return (current.get("organizationUuid") or "") == (pinned[1] or "")


def _clear_wiring_locked(switcher, path, unsplice=None) -> bool:
    """The read-modify-write of :func:`clear_wiring`, under its lock.

    ``unsplice`` is ``(pinned_email, identity)`` and only ``clear_pin``'s
    package-gone fallback passes it. It rides this function because the
    identity and the env block live in the same file under the same lock,
    and a second locked pass over both configs would be a second budget.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict):
        return False

    # THE PACKAGE'S HALF OF THE PIN, ON THE PATH WHERE THERE IS NO PACKAGE.
    # Left spliced, the config names the pinned account while nothing
    # records a pin, so `_live_login_identity` has no pin to un-splice
    # against and returns it literally -- and the next switch backs the live
    # credential up under that account's slot key.
    #
    # DECIDE ON THE ACCOUNT, NOT ON THE DICT, and only for a config that is
    # actually spliced. `identity_for_config` may answer a three-key roster
    # synthesis, so a whole-dict compare is never equal against a file
    # Claude Code maintains -- and rewriting on that verdict replaces one
    # cswap never spliced (the per-session config, which only the package
    # writes an identity into) and drops the fields CC owns there.
    unspliced = False
    if unsplice:
        pinned, identity = unsplice
        current = raw.get("oauthAccount")
        if identity and isinstance(current, dict) and _config_names_the_pin(
            switcher, current, pinned
        ):
            raw["oauthAccount"] = identity
            unspliced = True

    ours = _wire_mark_of(raw, path)
    if ours is None:
        # No wiring of ours, but the splice is still ours to undo.
        if unspliced:
            try:
                switcher._write_json(path, raw)
            except (OSError, ConfigError):
                pass
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
    # After the config write, never before: this is the receipt for what the
    # config still holds, and dropping it first then failing the config write
    # leaves the proxy vars unremovable except by hand. Gated on the sidecar
    # too, or `_wiring_present` reads the survivor and never converges.
    return _clear_ledger(path)


def _each_config(level: int = logging.DEBUG):
    """Both global configs, in read order, de-duplicated, guards applied.

    THE GETTER ITSELF CAN RAISE, which is why this is one function rather than
    three loops. ``get_default_global_config_path`` calls ``Path.home()``,
    which raises ``RuntimeError`` when HOME is unset and the uid has no
    ``/etc/passwd`` entry (the rootless-container shape). ``heal``'s contract
    is "never raises", and ``_wired_ports`` sits on the path from ``heal``
    through ``_wired_port_is_serving`` with no guard above it.

    A config this cannot even LOCATE has no opinion -- a fact about ONE config,
    never a reason to abandon the other.

    ``level`` is the caller's, and only ``clear_wiring`` raises it to WARNING.

    De-duplicated because the two getters return the SAME path whenever
    ``CLAUDE_CONFIG_DIR`` is unset.
    """
    from claude_swap.paths import (
        get_default_global_config_path,
        get_global_config_path,
    )

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


def _nonempty_mark(raw: object) -> list | None:
    """The receipt ``raw`` carries, only when it is a NON-EMPTY list.

    The single spelling of "this mapping claims a wiring". Two readers of the
    same marker once disagreed -- one accepted any truthy value, the other
    required a non-empty list -- so a malformed marker satisfied one and not
    the other and `--clear` never converged.
    """
    if not isinstance(raw, dict):
        return None
    mark = raw.get(_WIRE_MARK)
    return mark if isinstance(mark, list) and mark else None


def _saved_from(raw: object) -> dict:
    """The displaced values ``raw`` records, or ``{}``."""
    if not isinstance(raw, dict):
        return {}
    saved = raw.get(f"{_WIRE_MARK}Saved")
    return dict(saved) if isinstance(saved, dict) else {}


def _saved_of(raw: object, config_path=None) -> dict:
    """What the wiring displaced, from wherever the receipt lives.

    Same read-both rule as :func:`_wire_mark_of`, and it must stay paired with
    it: reading the marker from the sidecar and the displaced values from the
    config would restore one wiring's values over another's keys.
    """
    if config_path is not None:
        side = _read_ledger(config_path)
        # Paired with the MARKER, not merely with the sidecar's existence:
        # `_wire_mark_of` falls through to the config when the sidecar is empty
        # and the config carries a marker of its own, and taking the sidecar's
        # values there writes a proxy address that was never set.
        if _WIRE_MARK in side and not (
            _nonempty_mark(side) is None and _nonempty_mark(raw) is not None
        ):
            return _saved_from(side)
    return _saved_from(raw)


def _wire_mark_of(raw: object, config_path=None) -> list | None:
    """The marker THIS module wrote, or None. The single reader.

    The "read both locations" rule is written HERE, once, so every caller gets
    it without knowing the receipt moved; :func:`_nonempty_mark` holds the
    shape test both locations are judged by.
    """
    if config_path is not None:
        side = _read_ledger(config_path)
        ours = _nonempty_mark(side)
        if ours:
            return ours
        # An empty sidecar answers for the SIDECAR, not for the config. A
        # wiring written by an older cswap-pin carries a config key and no
        # sidecar; treating the empty sidecar as final made it invisible to
        # every recovery path while `.claude.json` still named a proxy port.
        # What it does rule out is resurrecting the receipt the clear emptied.
        if _WIRE_MARK in side and _nonempty_mark(raw) is None:
            return None
    return _nonempty_mark(raw)


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
        # Present-and-unreadable is not absent, even though both answer the
        # readers with `{}`. cswap-pin writes the receipt only here, so an
        # unreadable sidecar makes a LIVE wiring invisible to heal, purge and
        # --ensure at once. The return stays `{}`; what changes is that the
        # operator hears about it.
        _logger.warning(
            "%s exists but could not be read (%s), so it is treated as no pin "
            "receipt. If a pin IS wired, heal/purge/--ensure will all report "
            "nothing to do while the env block still names its proxy.",
            path, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _config_lock_is_free(budget: float) -> bool:
    """Can the config lock be taken within ``budget`` seconds?

    A probe, not a hold -- the caller re-locks immediately after. That race is
    deliberate: losing it costs one skipped unwire (the next launch heals it),
    while the alternative is the launch waiting on the package's own 5-second
    lock timeout, which it has no way to shorten.

    BOTH CONFIGS, because the operation this gates acts on both. Probing
    `get_global_config_path()` alone let a free session config and a HELD
    `~/.claude.json` pass, and `unwire_if_dead` then blocked on the package's
    `claude_config_lock(timeout=5)`: a 5.3s launch stall reached THROUGH the
    guard.

    The budget is per config rather than shared: two is the maximum, they are
    the same path whenever `CLAUDE_CONFIG_DIR` is unset, and splitting a
    sub-second budget makes each probe likelier to lose a race it would win.
    """
    from claude_swap.claude_locks import proper_lockfile

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
    # `stacklevel=3` attributes the record to the consumer --
    # `_wiring_present` / `_wired_ports` / `clear_wiring` -- rather than to
    # the shared `_each_config` generator, so the per-tick getters can be told
    # from the gated one. Three frames, not two, because the traversal is
    # shared.
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
    from claude_swap.update_check import _detect_install_method

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
        # No pin this launch, whatever the reason. Ask first and lock only if
        # there is work, and ask whether the port is DEAD rather than merely
        # wired: `_impl()` raising says nothing about the daemon. Per-config,
        # because the verdict is machine-wide and the act must not be.
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
            # also WROTE would leave a half-wired env in the object the caller
            # keeps (`session.py` passes the dict it goes on to use) and in the
            # one this function falls back to returning, which reaches
            # `os.execvpe` OUTSIDE the launch's try.
            wired = pin.wire_env(dict(env), port, ca_path)
            # Validated, not trusted: this reaches `os.execvpe`, which sits
            # outside the launch's try. `None` does not even fail -- it hands
            # the child the parent's environ, dropping CLAUDE_CONFIG_DIR.
            if isinstance(wired, dict) and all(
                isinstance(k, str) and isinstance(v, str) for k, v in wired.items()
            ):
                return wired
    except Exception:  # noqa: BLE001 — never block the launch
        pass
    # No proxy this launch. The env block is applied at boot, so a wiring a
    # previous launch left behind would send this child at a dead port. One
    # tail, not one per branch. Bounded: `unwire_if_dead` uses the package's
    # own 5s config lock and Claude Code holds that lock routinely, so if it
    # is not free right now, skip -- the next launch heals it.
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
        # find_spec has to IMPORT the parent package to read its __path__,
        # so a `cswap_pin/__init__.py` that raises surfaces here rather than
        # below. Swallowing it turns "your cryptography is broken" into
        # "install the package you already have". `e.name` tells them apart:
        # absent -> 'cswap_pin', broken root -> whatever it failed to import.
        if exc.name and not exc.name.startswith("cswap_pin"):
            raise
        found = False
    except ValueError:
        found = False
    if not found:
        raise ClaudeSwitchError(_install_hint())
    # NO RUNTIME VERSION FLOOR. The extra's floor lives in one place, the
    # `pin = ["cswap-pin>=X"]` requirement, exactly as the menubar extra
    # declares `rumps>=0.4.0` and then only asks whether the import works. A
    # hardcoded tuple here would need a pull request against THIS project for
    # every cswap-pin release, and a stale floor refuses a package the
    # installer just chose. Keeping a release out is an install-time job.
    return importlib.import_module("cswap_pin.proxy")


def _live_impl() -> ModuleType | None:
    """The implementation if it is usable RIGHT NOW, else None. Never raises.

    Both display helpers below need the same thing: resolve the package, and
    treat every failure as "no pin" rather than an error. Callers that ACT on
    the pin use :func:`_impl` instead and report what it raises -- hiding a
    broken install is right for a badge and wrong for a command.

    ``invalidate_caches`` because a long-lived process caches each sys.path
    directory by mtime, so an install landing inside the same mtime tick stays
    invisible without it.

    NOT MEMOISED, deliberately. A 1.0s TTL lived here and made the case it was
    meant to protect worse: an install landing inside the window stayed
    invisible until it expired. Measured, an uncached call is 185us against a
    3000ms poll interval. Cache it when a measurement shows a tick that
    matters, not because the shape looks expensive.
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


def pinned_email_recorded(switcher) -> str | None:
    """The pinned address as cswap's OWN record has it, for display.

    `pinned_email` asks the PACKAGE and answers None whenever it is absent, so
    the Cloud row said `none` while the badge beside it -- which reads the
    record through `pinned_identity` -- lit up on the same account. Every
    unwire except `clear_pin` leaves exactly that state, so it is where the
    row normally lands, not a corner.

    The address only, and only for a label: a badge must still go through
    `account_is_pinned`, because two slots may share one address.
    """
    identity = pinned_identity(switcher)
    return identity[0] if identity else None


def account_is_pinned(identity, email: str, org_uuid: str) -> bool:
    """Is ``(email, org_uuid)`` the pinned account?

    THE COMPOSITE, and it is the badge's whole correctness. Two managed slots
    may share one address across organizations, so an email-only test lights
    BOTH rows -- and `pin_is_broken`/`pin_is_applying` are then read against
    whichever row matched first, rendering a healthy pin as broken on the
    sibling or a dead one as clean.

    ``org_uuid`` is normalised, never dropped: a roster row imported before the
    org fields existed carries "", which must still match a pin whose org is
    also "" and must not match one that has an org.
    """
    return identity is not None and (email, org_uuid or "") == identity


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


def identity_move_is_not_a_login(switcher, before, now) -> bool:
    """Did `oauthAccount` move for a PIN reason rather than a `/login`?

    In the seam, not on the switcher: a second `def` of this name on
    `ClaudeAccountSwitcher` is a redefinition, not an overlay, and which body
    survives depends on the order two branches merge. Measured -- an upstream
    branch carrying a `return False` stub silently won over this branch's real
    body, each green alone.

    Under a pin the field has a second writer: the switch splices the pinned
    identity in, and the daemon's carry writes the account now signed in, so it
    swings between the two with nobody logging in. A guard that samples it
    twice and refuses on a difference refuses on the swing.

    Same discriminator as `ClaudeAccountSwitcher._live_login_identity`: the
    carry moves the CONFIG and nothing else, while a `/login` replaces the
    credential too. So the move is benign only when one of the two samples is
    the pinned identity AND the live credential is still the active slot's.

    FALSE when it cannot tell -- the safe direction here, and the opposite of
    `_live_login_identity`'s, because this answer only ever SUPPRESSES a
    refusal and a refusal writes nothing.
    """
    try:
        pinned = pinned_identity(switcher)
    except Exception:  # noqa: BLE001 — a tidy question must not raise here
        return False
    if not pinned:
        return False
    pair = tuple(pinned)[:2]
    if pair not in {tuple(t or ())[:2] for t in (before, now) if t}:
        return False        # neither sample is the pin — not our doing
    data = switcher._get_sequence_data() or {}
    recorded = data.get("activeAccountNumber")
    if recorded is None:
        return False
    slot = (data.get("accounts") or {}).get(str(recorded))
    if not isinstance(slot, dict) or not slot.get("email"):
        return False
    return switcher._live_credential_is(str(recorded), slot["email"]) is True


def pinned_slot(switcher) -> "str | None":
    """The roster slot the pin names, or None when it cannot be told.

    THE SEAM cswap core asks so it does not have to re-derive this. Its
    callers today are `_repin_if_pin_slot_refreshed`, and the autoswitch
    tick's `_bridge_owner_number`, which needs the pinned account's BEARER to
    see the bridges it is about to rename.

    Keeping the ROTATION off the pinned slot is a separate use this function
    would also serve and nothing wires yet; do not read this docstring as
    saying that it does.

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
    Two callers need it: the rollback, because by then the record has already
    been overwritten by the pin that failed; and ``set_pin``, because this
    argument is evaluated BEFORE ``apply_pin`` writes the record.

    ``num`` is a slot the caller already resolved, and passing it skips the
    lookup. `_resolve_account_identifier` RAISES when one address matches two
    slots -- cswap's documented personal+org pattern -- and the function-wide
    except turns that into ``None``, meaning "leave the config alone". So on
    exactly the roster the composite work exists for, the splice did nothing.

    It belongs here rather than in the package: `_pinned_email_now` must never
    ask the package, because the clear path has to work when the package is
    what is broken, and the remaining steps read cswap's OWN backup store.

    None on every doubt -- no pin, no stored config for it, an unreadable
    record. An optional feature must never be able to block a switch.
    """
    try:
        ident = pinned_identity(switcher)
        asked = email                    # an address the CALLER named
        email = email or (ident[0] if ident else None)
        if not email:
            return None
        if num is None and asked is None and ident:
            # The COMPOSITE, not the address, when the address is the pin's
            # own: `_resolve_account_identifier` raises on an address naming
            # two slots, the caller-wide `except` turns that into None, and
            # None here writes the account being switched TO as the bridge
            # owner. Only when `asked` is None -- a caller naming a different
            # address is asking about another account, whose org is not ours.
            num = _slot_for(switcher, email, ident[1] if len(ident) > 1 else None)
        num = num or switcher._resolve_account_identifier(email)
        if not num:
            return None
        raw = switcher._read_account_config(str(num), email)
        oauth = json.loads(raw).get("oauthAccount") if raw else None
        if isinstance(oauth, dict) and oauth.get("accountUuid"):
            return _fresher_remembered(switcher, oauth) or oauth
        # A machine that has never switched INTO the pinned account has no
        # stored config to copy, and None here makes `_perform_switch` fall
        # back to the account being switched TO. The roster answers: its
        # `uuid` IS what a stored config calls `accountUuid`. A stored config
        # still wins when it has one -- it also carries displayName, which the
        # roster does not.
        row = ((switcher._get_sequence_data() or {})
               .get("accounts", {}).get(str(num)) or {})
        uuid = (row.get("uuid") or "").strip()
        if not uuid:
            # Claude Code compares an owner on account uuid AND org uuid, so an
            # identity without one is no better than None — and None at least
            # means "leave the field alone".
            return oauth if isinstance(oauth, dict) and oauth else None
        out = {"emailAddress": row.get("email") or email,
               "organizationUuid": row.get("organizationUuid") or "",
               "accountUuid": uuid}
        # EVERY KEY MISSING HERE IS STRIPPED, because the splice replaces
        # `oauthAccount` whole -- and Claude Code answers a stripped
        # `organizationName` with an unguarded profile fetch. Omitted rather
        # than blanked when the roster has none: an empty string is a wrong
        # name, where absence lets CC fill it in.
        org_name = (row.get("organizationName") or "").strip()
        if org_name:
            out["organizationName"] = org_name
        return out
    except Exception:  # noqa: BLE001 — never block a switch
        return None


def _fresher_remembered(switcher, oauth: dict) -> "dict | None":
    """The daemon's copy of this identity when it is newer than the backup's.

    The stored backup's `profileFetchedAt` is as old as that slot's last
    login; the daemon refreshes what it remembers from the server. Splicing
    the older stamp re-opens Claude Code's profile fetch, which answers as
    the ACTIVE account and moves the field off the pin. Same account only,
    and None whenever the package cannot be asked.
    """
    try:
        kept = _ask("remembered_pin_identity", _certdir(switcher))
    except Exception:  # noqa: BLE001 — a switcher with no backup dir: no daemon copy
        return None
    if not isinstance(kept, dict) or \
            kept.get("accountUuid") != oauth.get("accountUuid"):
        return None

    def stamp(d):
        v = d.get("profileFetchedAt")
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    return kept if stamp(kept) > stamp(oauth) else None


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


def carry_live_pointers():
    """Point every live session's bridge record at the account now signed in.

    Called right after a switch writes `~/.claude.json`. The package's daemon
    does this too, on noticing that file move -- but only while it is running,
    so a switch made with the daemon down left every live session vetoed until
    it came back. The process that wrote the file can do it without waiting.

    None when the extra is absent or the call raised: the switch has already
    written both files by then, and an optional feature must not turn a
    completed switch into a reported failure.
    """
    return _ask("carry_live_pointers")


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
    proxy.json, pid and port all agreeing. Measured: all three read healthy
    while every request went out UNPINNED, because the daemon
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
        # Name the pin in the live config, exactly as `set_pin` does. Without
        # `identity=` the splice returns early, so the repair restores a
        # serving daemon while `~/.claude.json` still names whichever account
        # is active -- and Claude Code takes that field as the OWNER of every
        # bridge minted afterwards. Ask about `email`, the account this call
        # is re-pinning: the bare form reads the record, which only happens to
        # agree here because two readers share a file.
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
#
# THIS IS THE BUDGET REQUESTED, NOT THE CEILING OBSERVED, and the gap is not
# ours to close from here. `proper_lockfile` checks its deadline and THEN
# sleeps `0.25 + random() * 0.25` unclamped, so one acquisition can overrun by
# a full jittered sleep. The two consumers then differ: `_config_lock_is_free`
# spends this PER CONFIG (~2.0s across two), while `clear_wiring` treats it as
# a TOTAL split into fair shares (~1.5s by the same overrun). Neither is 0.5s.
#
# Clamping that sleep to the remaining budget is a one-line fix in
# `claude_locks.py`, which is CORE cswap and deliberately out of this branch's
# scope. Raising this number instead would be the wrong repair: it would make
# the launch wait longer rather than less. Left as the request it is, with the
# real ceiling named so nobody re-derives it from the constant.
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
    # A STRING, checked HERE. `settings.json` is a file a human edits, and a
    # non-string `pinnedEmail` is truthy all the way down to the one consumer
    # that calls `.casefold()` on it -- raising out of `clear_pin` AFTER the
    # record and the wiring are gone, so a clear that fully succeeded exits 1
    # blaming the optional package. `_port_of_config` fixed the identical
    # class at its own source for the same reason: treat it as "no opinion"
    # here and every downstream reader inherits the fix.
    if not isinstance(email, str) or not email:
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
    # Best-effort whenever there IS a pin to go back to; see below.
    unspliced = bool(before)
    if not before:
        try:
            _back = _live_login_for_config(switcher)
        except Exception:  # noqa: BLE001 — a name must not cost the rollback
            _back = None
    try:
        _impl().apply_pin(switcher, *(before or (None, None)))
        # And the config, which the record alone does not put back:
        # `apply_pin` splices `~/.claude.json` BEFORE it starts the proxy, so
        # a pin that failed to start has already written its account there.
        # Restoring a previous pin means that pin; restoring NOTHING means the
        # live login, exactly as `clear_pin` decides it -- without the second
        # case a failed FIRST pin left its own account named in a config
        # nobody is logged in as.
        if before:
            # Safe to ask now: `apply_pin` has just RESTORED this record, so
            # the lookup sees the state it is naming. The None case cannot,
            # which is why it is resolved above.
            _back = identity_for_config(
                switcher, email=before[0],
                num=_slot_for(switcher, before[0], before[1]))
        # PART OF THE VERDICT ONLY WHEN THERE IS NO PIN TO GO BACK TO.
        # Restoring a PREVIOUS pin leaves the record naming it, so a config
        # that lags is a worse pin and not a failed one -- best-effort, as
        # every other splice site treats it. Restoring NOTHING is a
        # different state: the record is cleared, so a config still naming
        # the pin that never started has no record to un-splice against,
        # and the next switch backs the live credential up under that
        # account's slot key. `splice_config_identity` SKIPS and returns
        # False on a contended config lock rather than raising, so reading
        # only the record announces that as a clean rollback.
        result = _impl().splice_config_identity(_back)
        # FALSE IS TWO ANSWERS. `splice_config_identity` returns it for a
        # write it SKIPPED and for a config that already names the identity
        # -- the success case. Reading the bool alone graded a clean
        # rollback as a failure and sent the user to check a state the code
        # could already disprove, which is the defect one frame up.
        unspliced = (
            unspliced or _back is None or bool(result)
            or _config_already_names(_back)
        )
    except Exception:  # noqa: BLE001 — the re-read below is the verdict
        pass
    return unspliced and _pinned_email_now(switcher) == before


def _config_already_names(identity: "dict | None") -> bool:
    """Does the global config already carry this identity? On the ACCOUNT.

    The package answers a skipped write and an already-correct config with
    the same `False`, and only the file can separate them.

    THE UUID ALONE, NOT THE ORG, when both sides carry one -- the same call
    `_config_names_the_pin` makes, and a DELIBERATE divergence from
    `_resolved_matches_slot_identity`, which keeps a lenient org conjunct
    ("uuid is globally unique; the org only corroborates"). Here the org
    would only ever narrow a verdict the uuid has already settled, and a
    wrong False is what strands the rollback message. The composite
    fallback below still carries the org, so the personal/org pair at one
    address stays separated on the path where no uuid is available.
    """
    if not identity:
        return False
    try:
        from claude_swap.paths import get_global_config_path

        raw = json.loads(get_global_config_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — an unreadable config decides nothing
        return False
    current = raw.get("oauthAccount") if isinstance(raw, dict) else None
    if not isinstance(current, dict):
        return False
    # THE STRONG KEY FIRST, as `_config_names_the_pin` decides the same
    # question. `identity_for_config` hands back a stored config VERBATIM,
    # so the identity itself can carry a non-string address: the composite
    # then blanks and declines about a config that matches byte for byte,
    # and `_restore_pin` reads that as a rollback that did not happen.
    uuid = identity.get("accountUuid")
    # AND THE CONFIG MUST HAVE ONE TOO, or the strong key is not available
    # and the composite is still the best evidence. `cswap add --token`
    # writes a stored config with a BLANK `accountUuid` while
    # `backfill_account_uuid` fills only the roster row, so the identity can
    # carry a uuid the config has never held -- and keying on the identity's
    # alone declines a config matching every field it actually has.
    if uuid and current.get("accountUuid"):
        return current.get("accountUuid") == uuid
    want = (_config_address(identity), identity.get("organizationUuid") or "")
    # A BLANK IS NOT A MATCH: with no uuid to fall back on, two unreadable
    # addresses would compare equal and invent an "already correct".
    if not want[0]:
        return False
    return (_config_address(current),
            current.get("organizationUuid") or "") == want


def _config_still_names(email: str, instead_of: "dict | None") -> bool:
    """A config still carrying ``email`` that is NOT what the clear meant.

    `clear_pin` decides on the record and the env block and NEITHER sees the
    `oauthAccount` splice, so every reason the un-splice did not happen ends
    at the same sentence a finished clear prints.

    ``instead_of`` is the identity the clear handed the un-splice, and it is
    what separates the two states: pinning the account you are logged in as
    is ordinary, and a FINISHED clear then leaves that same address named,
    correctly. The accountUuid tells them apart. An identity WITHOUT one
    cannot exempt anything, so the address alone decides and this warns --
    the safe direction here, because the cost of a wrong warning is a
    sentence and the cost of a wrong silence is a stranded config.

    AN UNREADABLE CONFIG COUNTS AS NAMING IT, matching `env_keys_survive`,
    the sibling reader that decides this same message: "I cannot check it"
    must not render as "it is clean" in the one sentence a purged user gets.

    AN ABSENT ONE DOES NOT -- a different question wearing the same
    `OSError`. `env_keys_survive` only ever iterates configs that WERE wired,
    hence existed; this walks every path `_each_config` can name.
    """
    want = (instead_of or {}).get("accountUuid")
    for path in _each_config():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue        # a config that is not there names nobody
        except (OSError, ValueError):
            return True     # cannot check is not clean -- see the docstring
        oauth = raw.get("oauthAccount") if isinstance(raw, dict) else None
        if not isinstance(oauth, dict):
            continue
        if _config_address(oauth) != email.casefold():
            continue
        if want and oauth.get("accountUuid") == want:
            continue        # this IS the identity the clear put there
        return True
    return False


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
# The env block cannot move: Claude Code reads it at boot, so that file IS the
# interface. The receipt can, and did -- it is bookkeeping only cswap reads,
# while `.claude.json` is the user's file. READ BOTH, WRITE NEW: a cswap-pin
# older than this change still writes the config key and must keep working, so
# this is two readers and one writer, not a migration with a cutover.


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
# The verdict lives here, not at each call site: these return `(ok, message)`
# the caller only has to render, so a divergence between the CLI and
# tui/dashboard.py needs someone to write a second copy of the logic rather
# than to forget a line.


def clear_pin(switcher) -> tuple[bool, str]:
    """Remove the pin AND its wiring. ``(ok, message)``.

    Both halves are re-read afterwards rather than inferred: ``apply_pin``
    cannot report on the wiring, and ``clear_wiring``'s bool is False both for
    "nothing to remove" and for "the lock was contended so this path was
    skipped" — only the second is a failure, and the skip is deliberate.
    """
    _pinned = _pinned_email_now(switcher)
    had_pin = _pinned is not None
    # Captured before the first thing that unwires, which is `apply_pin`, not
    # `clear_wiring`. Below both, the survivor check ran against a config the
    # package had already rewritten, and reported `(True, 'Unpinned the cloud
    # account')` over a config still naming a dead proxy port.
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
        _unsplice = False
    except Exception:  # noqa: BLE001 — this command must work when the pin does not
        # The record is cswap's own file, so clear it here rather than
        # reporting that the package could not. With the extra uninstalled,
        # leaving it made `--clear` fail, told the user to REINSTALL what they
        # had just removed, and re-pinned the old account the moment anything
        # did. `settings.json` -> `remoteControl` is as much cswap's file as
        # the wiring `clear_wiring` was moved here to remove.
        _clear_pin_record(switcher)
        _unsplice = True
    # AND THE SAME FALLBACK WHEN IT DID NOT RAISE. A peer whose `apply_pin`
    # RETURNS and clears nothing reaches the dead end the branch above exists
    # to prevent, without going through it: the record is still there, the re-
    # read below says "still pinned", and the user is told to "re-run once it
    # frees up" — advice that never converges, which is the exact wording the
    # comment above rejects. An older peer, or one whose pin backend is off,
    # does precisely this.
    if _pinned_email_now(switcher) is not None:
        _clear_pin_record(switcher)
        _unsplice = True
    # THE SPLICE IS THE OTHER HALF OF THE SAME STATE, and only the fallback
    # leaves it: a working `apply_pin` already un-spliced with this identity.
    cleared = clear_wiring(
        switcher,
        unsplice=(_pinned, _back_to)
        if (_unsplice and _pinned and _pinned[0] and _back_to)
        else None,
    )
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
    # ONE VERDICT, SEVERAL ENDINGS. Computed BEFORE the stale-receipt return,
    # because that branch fired first and dropped this entirely: an unwritable
    # `pin-wiring/` fails AFTER a successful config write, so the env keys are
    # already gone, `env_keys_survive` is empty, and nothing else in this
    # function can see the splice. Two endings that compete lose one.
    left_spliced = bool(
        _pinned and _pinned[0] and _config_still_names(_pinned[0], _back_to)
    )
    _also_stranded = (
        ". A config also still names the ex-pin as the logged-in account; "
        "switch to the account you want."
    )
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
            + (_also_stranded if left_spliced else "")
        )
    if left_spliced:
        # A DIFFERENT STATE AND A DIFFERENT SENTENCE, exactly like the stale
        # receipt above. The pin is gone and nothing dials a dead port, but a
        # config still names the ex-pin, so Claude Code keeps making sessions
        # owned by an account nothing is pinned to -- the state the un-splice
        # exists to prevent, reported until now as a plain success.
        #
        # NOT GATED ON THE AMBIGUITY DECLINE. Every route that leaves the
        # splice ends here, including the one where `_live_login_for_config`
        # answered None and there was no un-splice to attempt at all.
        return True, (
            "Unpinned the cloud account, but a config still names it as the "
            "logged-in account. Switch to the account you want, so new "
            "sessions stop being owned by the old one"
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
    # Refused here, not at the call sites. An API-key account can never be
    # pinned -- `sk-ant-api...` is not OAuth JSON, so the provider returns None
    # for every request and each fails open: daemon spawned, badge lit,
    # nothing pinned. The TUI's row filter is a courtesy, not the enforcement:
    # an open submenu is never rebuilt, so a row that was OAuth when drawn can
    # pin an API-key account when selected. A kind we cannot READ is refused
    # too -- swallowing the lookup is indistinguishable from no refusal.
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
        # Hand over the identity, do not apply it here. Once a pin is set the
        # live config must name it (`oauthAccount` is what Claude Code reads
        # to decide who owns a bridge) and that rule is pin functionality. The
        # LOOKUP cannot move: it reads cswap's backup store, whose layout the
        # package has no business knowing. Ask about `email`, not about the
        # record -- this argument is evaluated BEFORE `apply_pin` writes it,
        # so the no-argument form resolves the PREVIOUS pin.
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

    NOT MEMOISED, and a reviewer proposing to cache it should read this first.
    Every repeat sits AFTER something that can change the answer -- a restart,
    a clear whose lock was contended -- so a cache is precisely the inference
    those call sites refuse. The cost only bites on a port that DROPS: one
    with nothing on it refuses instantly.

    Asks the thing that is about to be removed, rather than any state file.
    ``proxy.json`` is unlinked at the START of a respawn, so its absence is not
    proof of death while the original daemon is still serving.

    Works with the extra absent or broken: a loopback connect, not an import.

    False when nothing is wired, when the port is unreadable, or when it
    refuses -- all of which mean "healing is allowed to proceed".

    ``_switcher`` is unused (see :func:`_wiring_present`) but kept, and
    underscore-prefixed, for the same call-compatibility reason.
    """
    # EVERY wired config must serve, not merely one of them. The two are
    # written asymmetrically -- `cswap_pin.wire_global_config` writes only the
    # session config while this reads both -- so answering on the first config
    # that serves lets a live session config mask a dead default one, and
    # plain `claude` from a terminal boots against the dead port while
    # `--heal` says "Nothing to heal". An unwired config is not a
    # counter-example: only a config that NAMES a port has an opinion.
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
    # Two states reach that list and they need different sentences. A config
    # whose receipt names keys the env no longer has is a LEFTOVER RECEIPT,
    # not a broken port value, and it is deterministic: `_ledger_path` keys
    # the sidecar on the config PATH, so a `.claude.json` deleted and
    # recreated inherits the same receipt.
    # Hoisted because it is loop-invariant and re-walks both configs and both
    # sidecars per unreadable path.
    receipts = wired_env_keys(switcher)
    # UNREADABLE IS NOT CLEAN -- the rule `env_keys_survive` states in those
    # words. `_env_of_config` returns None for a config it could not read, and
    # `or ()` turned that into an empty set: the intersection was then empty
    # for a reason that has nothing to do with the env block, and the config
    # was reported as a leftover receipt with "nothing is misrouted" over a
    # file nobody managed to look at.
    stale = [p for p in unreadable
             if (env := _env_of_config(p)) is not None
             and not (set(receipts.get(p, ())) & set(env))]
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

    ``lock_timeout`` bounds OUR config lock, and the PACKAGE's config lock too
    on a version that accepts it. It does not reach cswap-pin's SPAWN lock,
    which `impl.heal` takes with no timeout of ours to give it.

    Two outcomes, in order of preference:

    1. Restart the daemon on the SAME port. Live sessions are already wired to
       that address and their env is fixed at exec, so a daemon returning to it
       is picked up with no restart and nothing to reconnect.
    2. Failing that, REMOVE THE WIRING. Unpinned is a working session; wired to
       a dead port is not, because the stale wiring is applied at BOOT and new
       sessions inherit the dead port too.

    Never raises. Its callers are `cswap pin --ensure`, run from an rc file
    before every hand-launched ``claude``, and a hand-run ``cswap pin --heal``:
    a launch, or a repair someone is waiting on.

    ``connect_timeout`` is every loopback probe below, and the LAUNCH path must
    pass it -- one call arms three probes, so a port that black-holes rather
    than refuses costs the launch 4.2s on the ``--heal`` default.
    """
    # A serving pin is never TORN DOWN -- that is what the guard protects, and
    # the destructive operation is `clear_wiring` at the bottom, not the
    # restart. Returning on `serving` before `impl.heal()` runs makes a daemon
    # that serves its wired port while running code we no longer ship
    # unreachable, which is exactly what an upgrade leaves behind. So the
    # restart runs FIRST and the serving check gates only the unwire;
    # `impl.heal` returns False for "serving, wired and current" and rebinds
    # the same port when it recycles, so live sessions never see the swap.
    impl = _live_impl()
    if impl is not None:
        try:
            # `impl.heal` covers three states: a daemon that died, one
            # serving while the config names nothing, and one serving but
            # obsolete. Re-read the True as well as the False -- the package
            # is a peer on its own release schedule, so an impl returning True
            # while binding nothing would report success over a dead port.
            #
            # The signature is PROBED, not assumed: a version predating the
            # keyword raises TypeError inside this try, which would swallow
            # heal altogether. Neither signature view alone is safe -- a
            # `wraps` wrapper that DROPS keywords looks accepting when
            # followed, a transparent `(*a, **kw)` one when unfollowed.
            def _accepts(sig, name: str) -> bool:
                params = sig.parameters
                return name in params or any(
                    p.kind is inspect.Parameter.VAR_KEYWORD
                    for p in params.values())

            def _both(name: str) -> bool:
                # The unfollowed view wins when it NAMES the parameter,
                # because that is the signature the call binds against: a
                # compat shim that grows a keyword over an older inner accepts
                # it. Requiring both views to agree can only turn a yes into a
                # no. Agreement still decides a wrapper claiming only
                # `**kwargs`, where the inner is the only evidence there is.
                try:
                    outer = inspect.signature(impl.heal, follow_wrapped=False)
                    if name in outer.parameters:
                        return True
                    return (_accepts(inspect.signature(impl.heal), name)
                            and _accepts(outer, name))
                except (TypeError, ValueError):
                    return False

            _takes_identity = _both("identity")
            # Through `_slot_for`, like every other caller: the bare form
            # makes `identity_for_config` resolve an ADDRESS, and that raises
            # when one address names two slots (the personal+org roster this
            # composite key was built for). And the budget rides with it, or
            # the package waits ten times as long as this caller allows --
            # the splice inside `heal` takes the same config lock.
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
        # No package, so nothing can restart or recycle -- but a serving pin
        # is still a working one, and removing its wiring would unpin a
        # healthy session. The port the WIRING names is the right question,
        # not any state file: `_spawn_daemon` unlinks proxy.json as its first
        # act, so a missing record is not proof of death.
        return _nothing_to_heal(switcher)
    # No package, or the restart failed. Either way the wiring must not
    # outlive the daemon it points at, and `clear_wiring` works WITHOUT the
    # package on purpose. Ask `_dead_wired_configs`, not `_wiring_present`:
    # the latter keys on the marker alone, so a config carrying it with no
    # readable CSWAP_PIN_PORT would be torn down over a live proxy. Re-read
    # after the clear -- `clear_wiring`'s bool is False both for "nothing to
    # remove" and for "the lock was contended".
    try:
        # THE DEAD CONFIGS, NOT "THE WIRING". The list IS the same question
        # one bool wide, and asking it as a bool is what let a machine-wide
        # verdict authorise a machine-wide ACT: with the session config
        # live and the default config dead, `clear_wiring` stripped both and
        # unpinned sessions that were routed correctly. Take the list instead
        # and clear exactly what is dead — one probe round either way.
        dead = _dead_wired_configs(switcher, connect_timeout=connect_timeout)
        if dead:
            # Budgeted per caller, like `connect_timeout` beside it:
            # hardcoding the launch budget gave the human recovery command
            # 0.5s while Claude Code holds `.claude.json.lock` routinely.
            # Captured before the clear, against `dead` only, and read off the
            # ENV BLOCK rather than the marker -- a config left wired on
            # purpose is not a survivor.
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
            # Name the condition, not a cause this cannot know.
            # `clear_wiring` catches every exception around the lock, so one
            # message covered a held lock AND an unwritable config directory
            # and asserted the first for both -- on the permission shape
            # "re-run `cswap pin --heal`" can never come true. And say where
            # the cause is: `logging_config` attaches a console handler under
            # `--debug` alone, so on an ordinary run this line is all the user
            # sees.
            return False, (
                "A cloud pin wiring points at a proxy that is gone, and it "
                "could not be removed; re-run `cswap pin --heal`, or "
                "`cswap pin --heal --debug` for the reason (a held config "
                "lock and a config directory you cannot write both land here)"
            )
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not heal the cloud pin ({_safe(exc)})"
    # Refusing to act is not a reason to report the all-clear. A marker with
    # no readable `CSWAP_PIN_PORT` reaches here because `_dead_wired_configs`
    # declines to condemn it, correctly -- but declining leaves the wiring in
    # place, and `cswap pin --heal` is where this module's own messages send a
    # stranded user. Not a wolf: today's writer always emits the port, and
    # `--ensure` discards the message, so the launch path stays silent.
    return _nothing_to_heal(switcher)


def serving_port(switcher, *, connect_timeout: float = 2.0) -> int | None:
    """The port a live pin daemon is serving, or None. CSWAP'S OWN RECORD.

    Exists because nothing could ASK, and a consumer that cannot ask reaches
    into our data dir instead -- which makes our LAYOUT and SCHEMA a
    compatibility surface we cannot change without breaking scripts we do not
    own. A pinned session's ``HTTPS_PROXY`` names the pin's own dynamic port,
    so without this number such a session reads as bypassing whatever proxy
    sits behind it.

    Read from the record rather than through the package, like
    :func:`_pinned_email_now`: the caller most likely to need this is one
    diagnosing a failure, which is when the package may be what is broken.

    THE DAEMON'S RECORD, NOT THE CONFIG'S. "Which port is the proxy on" and
    "which port were sessions told to use" are the same number in the healthy
    case and deliberately not during a handover; `proxy.json` is what the
    daemon itself publishes.

    Liveness is not assumed: a recorded port whose daemon has died would send
    the caller to an address nothing serves, so the port is probed with a
    loopback connect, which also works with the package absent.
    """
    record = _certdir(switcher) / "proxy.json"
    try:
        port = int(json.loads(record.read_text(encoding="utf-8"))["port"])
    except Exception:  # noqa: BLE001 — absent/unreadable/malformed: no opinion
        return None
    if not 0 < port <= 65535:
        return None
    # BUDGETED BY THE CALLER: a port that DROPs rather than refuses costs the
    # whole timeout, which is what `_LAUNCH_PROBE_S` exists to bound.
    return port if _port_answers(port, connect_timeout) else None


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

    # EVERY BRANCH ABOVE `_impl()` RUNS WITHOUT THE PACKAGE, and that ordering
    # is the contract, not an accident: `--ensure`, `--set_port`, `--get_port`,
    # `--get_certdir`, `--heal` and `--clear` are the commands a user reaches
    # for when the pin is the broken thing. Each is answered from cswap's own
    # files. Only pinning itself needs the package, so only it resolves one.
    if ensure:
        # The launch contract, which `--heal` deliberately does not make. An
        # rc hook calls this before EVERY `claude`, so it never fails (every
        # path exits 0, a raise included), stays silent, and is cheap on a
        # machine that never pinned. The irreducible part is the TRIGGER: a
        # hand-launched `claude` execs from the user's shell, and nothing of
        # ours runs inside it.
        try:
            # NOTHING WIRED AND NOTHING RECORDED IS THE COMMON CASE.
            if not _wiring_present(switcher) and _pinned_email_now(switcher) is None:
                return 0
            # BUDGETED, like the two probes below it.
            heal(switcher, connect_timeout=_LAUNCH_PROBE_S,
                 lock_timeout=_LAUNCH_LOCK_BUDGET_S)
            # Re-read, do not trust `heal`'s return: only the config and a
            # connect can say whether a port is being served, and `cswap_pin`
            # is a peer on its own release schedule. Not dead code just
            # because `heal` ran above -- it clears the same dead set under
            # the same lock budget, so a config Claude Code holds through a
            # credential refresh leaves the verdict stale and drops to here.
            # Both the probe and the lock are budgeted, on a hook that runs
            # before every hand-launched `claude`.
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
        # Written here, not through the package: the cert dir is CSWAP's own
        # directory and this is a plain JSON record, so requiring cswap-pin to
        # save it would make the setting unsettable in exactly the case a user
        # is fixing something. Read-modify-write, because it is a SETTINGS
        # file and the next setting must not be erased by the next --set_port.
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
        # A number on stdout and nothing else: this is read by `$(cswap pin
        # --get_port)`, so a prefix, a colour code or a "no pin set" sentence
        # would land INSIDE the caller's variable. Silence plus a nonzero exit
        # lets a caller branch without string-matching.
        p = serving_port(switcher)
        if p is None:
            return 1
        print(p)
        return 0

    if get_certdir:
        # The state directory is not at the same path on Darwin as on Linux,
        # and a layout that cannot be ASKED for is one every consumer has to
        # SEARCH for. Unlike --get_port this does NOT probe: "where does this
        # host keep it" is true whether or not a daemon is up.
        print(_certdir(switcher))
        return 0

    if heal_only:
        # Exit 0 either way, so this is safe in a timer or a shell chain.
        # The budgets stay the HUMAN ones (`heal`'s 2.0s probe,
        # 9.0s lock) -- Claude Code holds `.claude.json.lock` routinely during
        # a credential refresh, and `--ensure` is the flag with the launch
        # budgets.
        changed, msg = heal(switcher)
        print(msg if changed else dimmed(msg))
        return 0

    if clear:
        # Any failure falls back, not just a
        # missing package -- "installed but unusable" is the other way a user
        # ends up here. The same `clear_pin` the TUI calls: one decision, one
        # implementation, two renderings.
        ok, msg = clear_pin(switcher)
        if not ok:
            warning(msg)
            return 1
        # Print what `clear_pin` decided. Rendering the returned message only
        # when it started with "No " and replacing every other one hid the
        # stale-receipt success, whose entire value is the PATH it names, from
        # the CLI while the TUI printed it verbatim. The accent stays on the
        # ordinary unpin, the only case whose wording this layer owns.
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
            _warn_if_bridges_disagree(pin, switcher)
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

    # A re-pin takes effect under the live proxy: the pinned account is
    # re-read per request. The one thing it cannot move is a Remote Control
    # session that is ALREADY open -- the server fixed its owner at creation,
    # so reconnecting inside it is what mints a new one. Name those sessions
    # instead of telling everyone to restart.
    #
    # A note must not fail the action: the pin is applied and "Pinned..." has
    # printed, so everything below is advice. Unguarded, a raise from the peer
    # turned a SUCCEEDED pin into an error telling the user to run `--clear`.
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


def _warn_if_bridges_disagree(pin, switcher) -> None:
    """Say so when the live bridges do not belong to the account we pinned.

    The status line reports the PIN, which is what we wrote, never what the
    machine has. Measured with three accounts at once: thirteen live bridges,
    none on the pinned org, and the command reported "pinned" throughout. What
    ended the silence was the server answering `API Error: 500` on a reattach,
    which cost the user the session.

    NEVER FATAL. This is the second unguarded call into the optional package
    on this path; the first turned a SUCCEEDED pin into `Error: ... not usable`
    with advice to run `--clear`, which would have destroyed it.
    `observed_bridge_owners` landed in cswap-pin 0.1.85 and the two ship on
    separate schedules, so a host on an older one must lose this extra line and
    keep the command.

    Only a bridge whose recorded owner DISAGREES is named. An unrecorded owner
    (`None`) is not evidence of anything and stays quiet.
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
    # Compare against the CONFIG, not the pin. `bridgeOwnerAccountUuid` has
    # two writers that mean opposite things: Claude Code records the bridge's
    # true server-side owner, and cswap-pin's live carry writes the account
    # now signed in so CC's own comparison agrees and it REATTACHES instead of
    # minting. This sentence asks the reattach question, and CC answers it by
    # comparing the stored pointer to `.claude.json`'s `oauthAccount` -- never
    # to the pin. `_get_current_account` over `_live_login_identity` for the
    # same reason: the latter UN-SPLICES the pin, and CC has no such notion.
    try:
        live = switcher._get_current_account()
    except Exception:  # noqa: BLE001 — a note must not fail the action
        return
    live_org = (live[1] if live and len(live) > 1 else "") or ""
    if not live_org:
        return
    # `oauthAccount` oscillates between the pin and the active login, so this
    # sentence would otherwise be decided by WHEN the command ran. The roster's
    # active slot does not oscillate. A bridge on NEITHER is the failure this
    # exists to catch and still warns; the gate stays narrow, which is what
    # keeps the carry itself honest.
    reachable = {live_org}
    try:
        row = ((switcher._get_sequence_data_migrated() or {})
               .get("accounts", {})
               .get(str(switcher.current_account_number()), {}))
        if row.get("organizationUuid"):
            reachable.add(row["organizationUuid"])
    except Exception:  # noqa: BLE001 — a note must not fail the action
        pass
    # `None` is dropped on purpose: an unrecorded owner is UNKNOWN, not a
    # disagreement, and `observed_bridge_owners` keeps the key so the two stay
    # distinguishable. Claiming a mismatch from unknown is the shape this
    # warning exists to catch, one level up.
    other = sorted({o for o in owners.values() if o and o not in reachable})
    if not other:
        return
    warning(
        "the live Remote Control bridges do not belong to it: "
        f"{len(other)} other organization(s) — {', '.join(other)}. "
        "A reattach against a bridge this login does not own is refused by "
        "the server; those sessions lose their history when they restart."
    )
