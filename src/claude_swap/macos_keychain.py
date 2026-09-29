"""macOS Keychain access via the ``security`` CLI.

A small wrapper around the system ``security`` tool for storing generic
passwords, used instead of the third-party ``keyring`` library. Two reasons:

- The macOS hot path no longer needs the ``keyring`` dependency.
- Keychain items are created and read by the same stable ``security`` binary, so
  reads stay silent across upgrades. ``keyring`` (and any in-process
  Security.framework call) anchors the item's access to the *Python interpreter*,
  which ``uv tool upgrade`` rebuilds — at which point macOS can show the "wants to
  use your keychain" prompt. ``security`` never changes, so creator == reader and
  there is no prompt.

The read/write/delete shapes mirror Claude Code's own implementation
(``utils/secureStorage/macOsKeychainStorage.ts``):

- ``set_password`` hex-encodes the value (``-X``) and pipes the command through
  ``security -i`` (stdin) so the secret never appears in process argv (a
  process-monitor / CrowdStrike concern). It falls back to argv only when the
  command would overflow ``security -i``'s 4096-byte stdin line buffer, which
  would otherwise truncate mid-argument and silently corrupt the entry.
- ``get_password`` uses ``find-generic-password ... -w`` and treats exit code 44
  as "not found" (returns ``None``); any *other* non-zero exit raises so callers
  can tell a genuine miss apart from a locked/denied/unavailable Keychain.

Caveat: values must be printable text. ``find-generic-password -w`` prints the
stored data raw only when it is printable; data with non-printable bytes comes
back *hex-encoded*, so a write/read round-trip would not be identity. Fine for
this codebase (credentials are ASCII JSON), but don't reuse this wrapper for
arbitrary binary data. Claude Code's ``-w`` reads share the same constraint.

This module is import-safe on every platform (it only shells out at call time);
its functions are only meaningful on macOS.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

# ``security -i`` reads stdin with a 4096-byte fgets() buffer (BUFSIZ on darwin).
# A command line longer than this is truncated mid-argument: it fails to write
# while leaving any previous entry intact (Claude Code #30337). 64 bytes of
# headroom guards against line-terminator accounting differences.
SECURITY_STDIN_LINE_LIMIT = 4096 - 64

_NOT_FOUND_RC = 44  # errSecItemNotFound surfaced by find/delete-generic-password

# Bound every ``security`` spawn so a wedged Keychain (a locked login keychain
# prompting for an unlock that never comes on a headless/SSH host) can't hang the
# CLI. 5s, deliberately short: a credential op that has to fall back to the file
# may be followed by a best-effort cleanup spawn, so the per-op budget doubles in
# the worst case. A healthy Keychain answers in well under 100ms.
_TIMEOUT = 5.0

# Pin the absolute path to Apple's system binary rather than resolving via PATH:
# this is a credential tool, so an attacker-controlled ``security`` earlier on
# PATH must not be able to intercept secrets. ``/usr/bin/security`` is present on
# every macOS.
_SECURITY = "/usr/bin/security"


class KeychainError(Exception):
    """A ``security`` invocation failed for a reason other than "not found"."""


# The exceptions a Keychain operation may raise that callers should treat as
# "Keychain unusable" (→ fall back to file storage) rather than a programming
# bug: a wrapper failure (KeychainError, incl. a converted timeout), a raw
# subprocess timeout, or a missing ``security`` binary (OSError). Catching this
# tuple — never bare ``Exception`` — keeps a real bug loud instead of silently
# routing to the file backend mid-invocation.
KEYCHAIN_ERRORS = (KeychainError, subprocess.TimeoutExpired, OSError)


def keychain_account_name() -> str:
    """Account name for the active-credential Keychain item, mirroring Claude
    Code's ``getUsername()`` (``utils/secureStorage/macOsKeychainHelpers.ts``).

    ``$USER`` first, then the OS username, then a stable final fallback. Matching
    this exactly matters on headless/launchd/cron hosts where ``$USER`` is unset:
    a divergent default (e.g. ``"user"``) would key a *different* Keychain item
    than Claude Code, so the two could not see each other's active credential.
    """
    user = os.environ.get("USER")
    if user:
        return user
    try:
        import pwd  # POSIX-only; the account-name call sites are macOS-only

        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        return "claude-code-user"


# Opt-in in-process memo of ``security`` reads (memory only, never on disk), so a
# steady-state poll costs one exec per item instead of one per pass: every exec
# is scanned by endpoint security software. A read consults or fills it ONLY
# inside ``memo_reads()``; everywhere else it execs exactly as it always did.
#
# The memo holds the answers of ONE keychain generation, named by a stamp of the
# ``*.keychain*`` files under ~/Library/Keychains (so a non-login default keychain
# counts too): every write to a keychain (ours or Claude Code's) moves
# ``st_mtime_ns`` (size alone does not: measured unchanged across two writes). The
# stamp is taken BEFORE the exec that produces an answer, so a write during the
# exec leaves that answer under the old stamp, where the next call drops it. A
# moved stamp clears the whole memo, which bounds its size and lifetime.
#
# A FAILED exec (locked, denied, timed out) forgets everything and ends the
# generation too: a locked keychain leaves its files untouched, so the stamp cannot
# say so, and once a failure has been seen every read executes until one succeeds.
# That is also why a memo hit never stands in for the success that ends a failure
# (``CredentialStore._kc_call`` clears its failure flag on any return): a hit only
# exists for an answer read after the last failure.
#
# No stat-able keychain file, no memo.
# ponytail: the data-protection keychain (~/Library/Keychains/<uuid>/) is not
# stamped: ``security`` reaches it only for items stored with that flag, which this
# tool never does. Stamp it too if that changes.
_KEYCHAIN_DIR = "~/Library/Keychains"
_memo: dict[tuple[str, str, str], object] = {}
_memo_stamp: tuple | None = None  # what every entry in _memo was read under
_epoch = 0  # bumped by every failed exec, so a straggler's answer cannot land
_lock = threading.Lock()
_local = threading.local()


def _stamp() -> tuple | None:
    root = os.path.expanduser(_KEYCHAIN_DIR)
    files = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if ".keychain" in entry.name:
                    st = entry.stat()
                    files.append((entry.name, st.st_ino, st.st_mtime_ns, st.st_size))
    except OSError:
        return None
    return (root, _epoch, tuple(sorted(files))) if files else None


def keychain_stamp() -> tuple | None:
    """The memo's stamp, for a caller that gates its own work on "did a keychain
    move": it changes on every write to a keychain and on every failed exec.
    ``None`` where there is no stat-able keychain file (not macOS, or unreadable)."""
    return _stamp()


def _read_stamp() -> tuple | None:
    """The stamp a read runs under, or ``None`` when it must not touch the memo:
    outside ``memo_reads()``, or inside ``fresh_reads()`` (which wins)."""
    if getattr(_local, "memo", False) and not getattr(_local, "fresh", False):
        return _stamp()


def _recall(key, stamp):
    """``(value,)`` when ``key`` was read under ``stamp``, else ``None``."""
    global _memo_stamp
    with _lock:
        if stamp[1] != _epoch:  # a failure came after this stamp was taken
            return None
        if stamp != _memo_stamp:
            _memo.clear()
            _memo_stamp = stamp
        if key in _memo:
            return (_memo[key],)


def _remember(key, stamp, value) -> None:
    with _lock:
        if stamp and stamp == _memo_stamp:  # else a write or a failure came first
            _memo[key] = value


def _forget(service: str, account: str) -> None:
    with _lock:
        for kind in ("pw", "mdat"):
            _memo.pop((kind, service, account), None)


def _invalidate() -> None:
    """A failed exec: forget every answer and end the generation (see above)."""
    global _epoch, _memo_stamp
    with _lock:
        _epoch += 1
        _memo.clear()
        _memo_stamp = None


@contextmanager
def _scope(flag: str):
    prev = getattr(_local, flag, False)
    setattr(_local, flag, True)
    try:
        yield
    finally:
        setattr(_local, flag, prev)


def memo_reads():
    """Reads on this thread inside the block may be served from the memo.

    For display and evaluation phases only. A switch, a refresh, a grant POST or
    a credential write reads the Keychain as it is now: it runs outside the
    block, or inside ``fresh_reads()``. A one-time-use refresh token POSTed on a
    memoized read of an item that has since locked cannot have its successor
    written back. Usable as a decorator too.

    The one write inside such a phase is the collect's ``_sweep_unclaimed_stash``,
    which acts on memoized slot backups (``slot_creds``). That is safe only
    because a locked item raises instead of reading, and the stamp moves on every
    keychain write: a hit is never older than the last write.
    """
    return _scope("memo")


def fresh_reads():
    """Reads inside the block skip the memo, even nested in ``memo_reads()``."""
    return _scope("fresh")


def _quote(value: str) -> str:
    """Quote a value for a ``security -i`` stdin command line.

    ``security -i`` re-parses each line shell-style, so wrap the value in double
    quotes and backslash-escape any embedded ``"``/``\\`` (e.g. the active-
    credential service name contains a space).
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def get_password(service: str, account: str) -> str | None:
    """Return the stored password, or ``None`` if no such item exists (rc 44).

    Raises :class:`KeychainError` on any other non-zero exit (locked / denied /
    unavailable) or a timeout, so a genuine miss is not confused with a transient
    failure. Only these two definite answers are memoized, never an error.
    """
    stamp = _read_stamp()  # before the exec, see the memo note above
    key = ("pw", service, account)
    if stamp and (hit := _recall(key, stamp)):
        return hit[0]
    try:
        result = subprocess.run(
            [_SECURITY, "find-generic-password", "-a", account, "-w", "-s", service],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        _invalidate()
        raise KeychainError(
            f"security find-generic-password timed out after {_TIMEOUT}s"
        ) from e
    except OSError:  # a spawn that failed is a failed exec too
        _invalidate()
        raise
    if result.returncode == 0:
        # `-w` prints the value followed by one newline; strip exactly that so
        # values with meaningful leading/trailing whitespace survive intact.
        value = result.stdout.removesuffix("\n")
        _remember(key, stamp, value)
        return value
    if result.returncode == _NOT_FOUND_RC:
        _remember(key, stamp, None)
        return None
    _invalidate()
    raise KeychainError(
        f"security find-generic-password failed (rc={result.returncode}): "
        f"{result.stderr.strip()}"
    )


def item_exists(service: str, account: str) -> bool:
    """Whether a generic-password item exists, without touching its secret.

    Attribute-only lookup (no ``-w``): nothing is decrypted, so this can never
    trigger a Keychain prompt, even for items owned by another app. Returns
    ``True`` only on rc 0; "not found" (rc 44), error exits, a timeout, and a
    missing binary all return ``False``. Deliberately **non-raising**: callers use
    it for cleanup verification, not access decisions, so it must never feed the
    capability cache (a timeout here means "couldn't tell", not "Keychain works").
    A failed exec (not rc 44) still ends the read memo's generation, like any other.
    """
    try:
        result = subprocess.run(
            [_SECURITY, "find-generic-password", "-a", account, "-s", service],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        _invalidate()
        return False
    if result.returncode not in (0, _NOT_FOUND_RC):
        _invalidate()
    return result.returncode == 0


_MDAT_RE = re.compile(r'"mdat"<timedate>=0x[0-9A-Fa-f]+\s+"(\d{14})Z')


def item_modified_at(service: str, account: str) -> float | None:
    """UTC epoch of the item's Keychain ``mdat`` (last-modified) attribute.

    Attribute-only lookup (no ``-w``): nothing is decrypted. Non-raising —
    absent item, parse failure, timeout or a missing binary all return
    ``None``, since this feeds a freshness comparison where "don't know"
    must never be read as a claim. Only a parsed answer is memoized.
    """
    stamp = _read_stamp()
    key = ("mdat", service, account)
    if stamp and (hit := _recall(key, stamp)):
        return hit[0]
    try:
        result = subprocess.run(
            [_SECURITY, "find-generic-password", "-a", account, "-s", service],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        _invalidate()
        return None
    if result.returncode != 0:
        if result.returncode != _NOT_FOUND_RC:
            _invalidate()
        return None
    match = _MDAT_RE.search(result.stdout)
    if not match:
        return None
    try:
        mdat = datetime.strptime(
            match.group(1), "%Y%m%d%H%M%S"
        ).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    _remember(key, stamp, mdat)
    return mdat


def set_password(service: str, account: str, password: str) -> None:
    """Create or update a generic-password item (``-U``).

    Prefers ``security -i`` stdin so the secret stays out of argv; falls back to
    argv only for payloads that would overflow the stdin line buffer. Raises
    :class:`KeychainError` on a non-zero exit or a timeout.
    """
    hex_value = password.encode("utf-8").hex()
    # `-X` passes the value as hex, avoiding any escaping issues for the secret.
    command = (
        f"add-generic-password -U -a {_quote(account)} -s {_quote(service)} "
        f"-X {hex_value}\n"
    )
    try:
        if len(command.encode("utf-8")) <= SECURITY_STDIN_LINE_LIMIT:
            result = subprocess.run(
                [_SECURITY, "-i"],
                input=command,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
            )
        else:
            # Overflows the stdin line buffer; fall back to argv. Hex in argv is
            # recoverable by a determined observer but defeats naive plaintext-grep
            # rules, and the alternative — silent corruption — is strictly worse.
            result = subprocess.run(
                [
                    _SECURITY, "add-generic-password", "-U",
                    "-a", account, "-s", service, "-X", hex_value,
                ],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
            )
    except subprocess.TimeoutExpired as e:
        _invalidate()
        raise KeychainError(
            f"security add-generic-password timed out after {_TIMEOUT}s"
        ) from e
    except OSError:
        _invalidate()
        raise
    finally:
        _forget(service, account)  # a memo reader may have re-read the old value
    if result.returncode != 0:
        _invalidate()
        raise KeychainError(
            f"security add-generic-password failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )


def delete_password(service: str, account: str) -> None:
    """Delete a generic-password item. rc 44 (already absent) counts as success.

    Raises :class:`KeychainError` on any other non-zero exit or a timeout.
    """
    try:
        result = subprocess.run(
            [_SECURITY, "delete-generic-password", "-a", account, "-s", service],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        _invalidate()
        raise KeychainError(
            f"security delete-generic-password timed out after {_TIMEOUT}s"
        ) from e
    except OSError:
        _invalidate()
        raise
    finally:
        _forget(service, account)  # a memo reader may have re-read the old value
    if result.returncode in (0, _NOT_FOUND_RC):
        return
    _invalidate()
    raise KeychainError(
        f"security delete-generic-password failed (rc={result.returncode}): "
        f"{result.stderr.strip()}"
    )
