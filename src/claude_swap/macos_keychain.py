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
import shutil
import subprocess

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


def _quote(value: str) -> str:
    """Quote a value for a ``security -i`` stdin command line.

    ``security -i`` re-parses each line shell-style, so wrap the value in double
    quotes and backslash-escape any embedded ``"``/``\\`` (e.g. the active-
    credential service name contains a space).
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def get_password(
    service: str, account: str, *, keychain: str | None = None
) -> str | None:
    """Return the stored password, or ``None`` if no such item exists (rc 44).

    Raises :class:`KeychainError` on any other non-zero exit (locked / denied /
    unavailable) or a timeout, so a genuine miss is not confused with a transient
    failure.

    ``keychain``, when given, targets one specific keychain file instead of the
    default search list — appended as ``find-generic-password``'s trailing
    positional ``[keychain]`` argument (there is no ``-k`` flag for this
    subcommand; ``-k`` on ``security`` means something else entirely depending
    on the verb, e.g. a keychain *password* for
    ``set-generic-password-partition-list``). ``None`` (the default) preserves
    the exact prior behavior — search list, no positional argument at all.
    """
    args = [_SECURITY, "find-generic-password", "-a", account, "-w", "-s", service]
    if keychain:
        args.append(keychain)
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise KeychainError(
            f"security find-generic-password timed out after {_TIMEOUT}s"
        ) from e
    if result.returncode == 0:
        # `-w` prints the value followed by one newline; strip exactly that so
        # values with meaningful leading/trailing whitespace survive intact.
        return result.stdout.removesuffix("\n")
    if result.returncode == _NOT_FOUND_RC:
        return None
    raise KeychainError(
        f"security find-generic-password failed (rc={result.returncode}): "
        f"{result.stderr.strip()}"
    )


def item_exists(
    service: str, account: str, *, keychain: str | None = None
) -> bool:
    """Whether a generic-password item exists, without touching its secret.

    Attribute-only lookup (no ``-w``): nothing is decrypted, so this can never
    trigger a Keychain prompt, even for items owned by another app. Returns
    ``True`` only on rc 0; "not found" (rc 44), error exits, a timeout, and a
    missing binary all return ``False``. Deliberately **non-raising**: callers use
    it for cleanup verification, not access decisions, so it must never feed the
    capability cache (a timeout here means "couldn't tell", not "Keychain works").

    ``keychain``, when given, scopes the lookup to one keychain file — see
    :func:`get_password` for the positional-argument shape.
    """
    args = [_SECURITY, "find-generic-password", "-a", account, "-s", service]
    if keychain:
        args.append(keychain)
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def set_password(
    service: str,
    account: str,
    password: str,
    *,
    keychain: str | None = None,
    trusted_apps: list[str] | None = None,
) -> None:
    """Create or update a generic-password item (``-U``).

    Prefers ``security -i`` stdin so the secret stays out of argv; falls back to
    argv only for payloads that would overflow the stdin line buffer. Raises
    :class:`KeychainError` on a non-zero exit or a timeout.

    ``keychain`` targets one specific keychain file (see :func:`get_password`
    for the positional-argument shape); ``None`` writes to the default
    keychain, unchanged from before.

    ``trusted_apps``, given and non-empty, grants those application paths ACL
    access too (each becomes a ``-T`` entry, alongside this tool's own path so
    a later read/update through this wrapper keeps working). ``None``/``[]``
    is a byte-for-byte no-op vs. the pre-existing plain ``-U``, no ``-T`` —
    which leaves a freshly-created item trusting only ``security`` itself, so
    a headless in-process Security.framework reader (e.g. Claude Code) needs a
    GUI prompt to get in (issue #279).

    ``-U`` with ``-T`` does not safely widen an **already-existing** item's
    ACL in place: it routes through ``SecKeychainItemSetAccess``, which needs
    interactive authorization and can raise that same GUI prompt even against
    an unlocked keychain (proven in ``tests/test_macos_keychain_contract.py``).
    So when ``trusted_apps`` is given and the item already exists, this
    deletes and recreates it instead — a brand-new item's ACL is set at
    creation time, no prompt.
    """
    apps: list[str] = []
    if trusted_apps:
        for app in (_SECURITY, *trusted_apps):
            if app and app not in apps:
                apps.append(app)
        if item_exists(service, account, keychain=keychain):
            delete_password(service, account, keychain=keychain)

    hex_value = password.encode("utf-8").hex()
    # `-X` passes the value as hex, avoiding any escaping issues for the secret.
    parts = [
        "add-generic-password", "-U",
        "-a", _quote(account), "-s", _quote(service),
        "-X", hex_value,
    ]
    for app in apps:
        parts += ["-T", _quote(app)]
    if keychain:
        parts.append(_quote(keychain))
    command = " ".join(parts) + "\n"
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
            argv = [
                _SECURITY, "add-generic-password", "-U",
                "-a", account, "-s", service, "-X", hex_value,
            ]
            for app in apps:
                argv += ["-T", app]
            if keychain:
                argv.append(keychain)
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
            )
    except subprocess.TimeoutExpired as e:
        raise KeychainError(
            f"security add-generic-password timed out after {_TIMEOUT}s"
        ) from e
    if result.returncode != 0:
        raise KeychainError(
            f"security add-generic-password failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )


def delete_password(
    service: str, account: str, *, keychain: str | None = None
) -> None:
    """Delete a generic-password item. rc 44 (already absent) counts as success.

    Raises :class:`KeychainError` on any other non-zero exit or a timeout.

    ``keychain``, when given, scopes the delete to one keychain file — see
    :func:`get_password` for the positional-argument shape.
    """
    args = [_SECURITY, "delete-generic-password", "-a", account, "-s", service]
    if keychain:
        args.append(keychain)
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise KeychainError(
            f"security delete-generic-password timed out after {_TIMEOUT}s"
        ) from e
    if result.returncode in (0, _NOT_FOUND_RC):
        return
    raise KeychainError(
        f"security delete-generic-password failed (rc={result.returncode}): "
        f"{result.stderr.strip()}"
    )


def resolve_trusted_claude_apps() -> list[str]:
    """Resolve the local Claude Code executable path(s) to trust for Keychain
    ACL access (issue #279; see ``set_password``'s ``trusted_apps``).

    ``claude`` on ``PATH`` may be a real Mach-O binary, or an npm-installed
    shebang shim (``#!/usr/bin/env node ...``) that re-execs into node.
    macOS's ACL match is against the binary that actually calls into
    Security.framework at runtime, not ``argv[0]`` — trusting only the shim's
    path grants it nothing — so this also resolves and adds the interpreter
    for the shebang case. Returns ``[]`` when ``claude`` isn't on ``PATH``,
    so a caller that always passes this through stays a no-op there.
    """
    claude_path = shutil.which("claude")
    if not claude_path:
        return []
    resolved = os.path.realpath(claude_path)
    apps = [resolved]
    try:
        with open(resolved, "rb") as f:
            head = f.read(2)
        if head == b"#!":
            with open(resolved, "r", encoding="utf-8", errors="ignore") as f:
                shebang = f.readline().strip()
            tokens = shebang[2:].split()
            if tokens:
                interpreter = tokens[0]
                # `#!/usr/bin/env node` names the interpreter as env's own
                # argument, not the shebang path itself.
                if os.path.basename(interpreter) == "env" and len(tokens) > 1:
                    interpreter = shutil.which(tokens[1]) or tokens[1]
                apps.append(os.path.realpath(interpreter))
    except OSError:
        pass  # unreadable executable; trust just the resolved path we have
    deduped: list[str] = []
    for app in apps:
        if app not in deduped:
            deduped.append(app)
    return deduped
