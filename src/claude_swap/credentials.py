"""Credential storage layer for claude-swap.

Owns *where* credentials live and *how* they are read/written — the macOS
Keychain-vs-file routing, per-process capability detection and sticky fallback,
and the ``.enc``-wins backup reconciliation that landed in #66. Split out of
``switcher.py`` so the switcher reads as account orchestration again.

``CredentialStore`` is a leaf collaborator: it imports only the OS-primitive and
path helpers (``macos_keychain``, ``paths``) and never imports ``switcher``. It
reads its live configuration (``platform``, ``_logger``, ``credentials_dir``)
from a host *view* — a small data-only window onto the switcher that constructs
it — and must never call a switcher *method* through that host, or storage and
orchestration would re-couple. The store owns only its two pieces of state:
``_keychain_usable_cache`` (sticky, process-local) and
``_last_active_credentials_backend`` (for the post-switch follow-up message).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple, Protocol

from claude_swap import macos_keychain
from claude_swap.exceptions import (
    CredentialError,
    CredentialReadError,
    CredentialWriteError,
)
from claude_swap.fsutil import replace_with_retry
from claude_swap.models import Platform
from claude_swap.paths import (
    get_claude_config_home,
    get_credentials_path,
    get_default_claude_config_home,
    get_global_config_path,
)

_logger = logging.getLogger("claude-swap")


def _active_profile_is_default() -> bool:
    """Whether the active config home is the default profile's.

    ``CLAUDE_CONFIG_DIR`` pointed at the default profile is still the default
    profile, so this keys on where the path resolves rather than on the
    variable being set. Resolution also collapses symlinks, which is how a
    profile reached through one shows up as itself.

    Unresolvable paths answer False: treating an unknown profile as the default
    is what licenses reading another account's credential, and that is the
    failure this guards.
    """
    try:
        return (
            get_claude_config_home().resolve()
            == get_default_claude_config_home().resolve()
        )
    except Exception:
        return False


def _active_oauth_keychain_services() -> list[str]:
    """Keychain services holding the OAuth credential for the active environment.

    Resolved exactly the way :meth:`ClaudeAccountSwitcher._read_capture_credentials`
    resolves it, so the active read and the capture read agree about which
    profile's store they are looking at. Claude (2.1.220
    ``getMacOsKeychainStorageServiceName``) sources secure storage from
    ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` when that is *defined*, else
    ``CLAUDE_CONFIG_DIR``; defined-but-empty selects the *default* secure store,
    whose item is the unsuffixed one.

    Returned in try-order rather than as a single name for one case: an explicit
    ``CLAUDE_CONFIG_DIR`` naming the default profile. Claude hashes the exported
    string, so it would write a *suffixed* item there, but a user who has always
    used the default profile may only have the unsuffixed one. Capture handles
    that with the same fallback (``_same_directory`` → ``_read_credentials``).
    A defined ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` gets no fallback: it names the
    only store claude will read for this environment, so a miss means claude
    sees a logged-out profile and reaching into another store would report a
    credential claude is not using.
    """
    # Local import: session imports from this module, so a top-level import
    # would close the cycle. _read_capture_credentials does the same.
    from claude_swap.session import keychain_service_name

    secure_env = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    if secure_env is not None:
        if not secure_env:
            return [CLAUDE_CODE_KEYCHAIN_SERVICE]
        return [keychain_service_name(secure_env)]

    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir:
        return [CLAUDE_CODE_KEYCHAIN_SERVICE]

    services = [keychain_service_name(config_dir)]
    if _active_profile_is_default():
        services.append(CLAUDE_CODE_KEYCHAIN_SERVICE)
    return services


# Service name for per-account backup credentials now managed via the ``security``
# CLI on macOS. Deliberately distinct from KEYRING_SERVICE so old keyring items and
# new security items coexist during migration (safe write → verify → delete).
SECURITY_SERVICE = "claude-swap"

# Service name of Claude Code's *active* OAuth credential in the macOS Keychain
# (read by Claude Code itself; we read/write it when switching accounts).
CLAUDE_CODE_KEYCHAIN_SERVICE = "Claude Code-credentials"

# Service name of Claude Code's *active* managed API key (``/login`` with an
# ``sk-ant-api…`` key) in the macOS Keychain. Distinct from the OAuth service above
# (no ``-credentials`` suffix); Claude Code resolves it on a separate auth axis
# (``getApiKeyFromConfigOrMacOSKeychain``). On non-macOS the managed key instead
# lives in ``~/.claude.json`` as ``primaryApiKey`` (see below).
CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE = "Claude Code"

# Bounded retry for the active OAuth-credential Keychain read. A locked/contended
# login Keychain can fail a single `security` call transiently — e.g. just after
# wake while the keychain is still settling, or under contention with Claude Code's
# own statusline polling the same item — and a second attempt a moment later
# usually succeeds. This is an I/O backoff between retries of an external CLI, NOT
# a sleep papering over an internal race.
_ACTIVE_READ_ATTEMPTS = 2
_ACTIVE_READ_RETRY_DELAY = 0.3  # seconds between attempts

# After a Keychain failure the store drops to file mode so one CLI invocation
# can't split-brain between backends. A long-running daemon (menu bar / TUI)
# instead re-probes this long after the last failure: far longer than any CLI
# command runs (so the guarantee holds — a sub-second command never re-probes),
# short enough that a transient `security` timeout self-heals within a minute
# instead of disabling the Keychain for the whole process lifetime.
KEYCHAIN_RECHECK_COOLDOWN_S = 60.0


class ActiveCredentials(NamedTuple):
    """Outcome of reading Claude Code's active credential.

    ``value`` is the credential string (OAuth JSON or a raw managed key), ``""``
    when none exists in any backend, or ``None`` on a plaintext-file read error.
    ``keychain_unavailable`` is True only when the macOS OAuth Keychain read failed
    (locked / denied / timeout) and nothing else covered it — letting callers
    distinguish a transiently unreadable Keychain from a genuinely empty slot,
    instead of collapsing both into a misleading "no credentials".
    ``degraded`` is True whenever the OAuth Keychain read failed, even when a
    fallback covered it: the bytes served may then be a stale generation (on
    macOS Claude Code rotates keychain-only, so the plaintext file can lag).
    A degraded credential may be adopted or served, but its refresh token
    must never be consumed — POSTing a superseded one-time rt yields
    invalid_grant and a false dead-token strike on a live account.
    """

    value: str | None
    keychain_unavailable: bool
    degraded: bool = False


def looks_like_api_key(credentials: str | None) -> bool:
    """Whether a stored active credential is a raw managed API key vs OAuth JSON.

    Strict on purpose: a managed key is a bare ``sk-ant-api…`` string, while every
    OAuth/setup-token credential is a JSON object (``{"claudeAiOauth": …}``). Requiring
    the ``sk-ant-api`` prefix (and that it isn't JSON) keeps a raw/garbled
    ``sk-ant-oat…`` setup token from ever being misclassified as an API key.
    """
    if not credentials:
        return False
    text = credentials.strip()
    return text.startswith("sk-ant-api") and not text.startswith("{")


def _credential_object(credentials: str | None) -> dict | None:
    """Parse a JSON credential object, excluding managed API keys."""
    if not credentials or looks_like_api_key(credentials):
        return None
    try:
        data = json.loads(credentials)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


# The credential object's siblings of claudeAiOauth are not uniformly owned:
# these keys hold machine-shared OAuth integrations that rotate independently
# of any account slot, so on activation the live copy is authoritative.
# Everything else — known (trustedDeviceToken is enrolled per-account and
# re-enrolled on every login) or unknown — stays with the target slot: a
# stale restore of an unlisted shared field merely re-prompts for auth,
# while carrying a live account-bound field across a switch would present
# one account's credential under another.
SHARED_CREDENTIAL_KEYS = frozenset({
    "mcpOAuth",
    "mcpOAuthClientConfig",
    "mcpXaaIdp",
    "mcpXaaIdpConfig",
    "pluginSecrets",
})

# Account-scoped siblings cswap knows about, named so the unrecognized-key
# probe below doesn't flag them: claudeAiOauth is the login itself,
# trustedDeviceToken is enrolled per (device, account) at /login.
ACCOUNT_CREDENTIAL_KEYS = frozenset({
    "claudeAiOauth",
    "trustedDeviceToken",
})


def shared_credential_fields(credentials: str | None) -> dict | None:
    """Return the machine-shared fields of a Claude OAuth credential object.

    Only the ``SHARED_CREDENTIAL_KEYS`` allowlist is machine-shared; other
    siblings of ``claudeAiOauth`` are account-scoped or unknown and stay
    slot-owned. ``None`` means the input is not a JSON credential object
    (missing, malformed, or a managed API key). A dictionary — including
    ``{}`` — is authoritative for every allowlisted key: a key absent here
    is absent from the machine's current shared state.
    """
    data = _credential_object(credentials)
    if data is None:
        return None
    if "claudeAiOauth" in data:
        # A sibling key cswap doesn't know defaults to slot-owned (fails
        # safe), but silently: if Claude Code grows a new *shared* key,
        # that default quietly reintroduces the stale-restore papercut for
        # it — leave a trace so it gets noticed.
        unrecognized = data.keys() - SHARED_CREDENTIAL_KEYS - ACCOUNT_CREDENTIAL_KEYS
        if unrecognized:
            _logger.debug(
                "Live credential has sibling keys cswap does not recognize "
                "(a newer Claude Code?), treating them as slot-owned: %s",
                sorted(unrecognized),
            )
    return {key: data[key] for key in SHARED_CREDENTIAL_KEYS if key in data}


def merge_shared_credential_fields(
    target_credentials: str, shared_fields: dict
) -> str:
    """Compose a target Claude login with the machine's shared fields.

    The allowlisted keys are wholly live-owned, presence and absence alike:
    the target's copies are discarded and ``shared_fields`` supplies the
    current generation, so a shared key the machine no longer holds is not
    resurrected from the slot's snapshot. All other target fields pass
    through untouched. Returns ``target_credentials`` unchanged when it is
    not a JSON credential object carrying a Claude login (managed API keys
    and opaque legacy shapes stay activatable verbatim).
    """
    target = _credential_object(target_credentials)
    if target is None or "claudeAiOauth" not in target:
        return target_credentials

    composed = {
        key: value
        for key, value in target.items()
        if key not in SHARED_CREDENTIAL_KEYS
    }
    composed.update(shared_fields)
    return json.dumps(composed)


def approved_form(api_key: str) -> str:
    """The value Claude Code stores in ``customApiKeyResponses.approved``.

    Mirrors Claude Code's ``normalizeApiKeyForConfig`` (``apiKey.slice(-20)``): the
    last 20 chars. Storing anything else makes Claude Code's "is this key approved?"
    check miss and re-prompt the user to approve the key.
    """
    return api_key.strip()[-20:]


class _StoreHost(Protocol):
    """The live configuration view ``CredentialStore`` reads from its owner.

    Data only — the store reads these attributes at call time so post-construction
    overrides (e.g. tests setting ``switcher.platform``) are honored. The store
    must not reach for any *method* here.
    """

    platform: Platform
    credentials_dir: Path
    _logger: logging.Logger


class CredentialStore:
    """Owns the active and per-account backup credential stores.

    One store per switcher: the capability cache is per-process, learned from real
    ``security`` calls, and a fresh process re-evaluates from scratch.
    """

    def __init__(self, host: _StoreHost):
        self._host = host
        # macOS Keychain usability, learned per-process from real `security`
        # calls (see _kc_call / _use_keychain). None = not yet probed; True/False
        # once an op has run. _last_active_credentials_backend records where the
        # most recent active-credential write landed ("keychain" | "file"), for the
        # post-switch follow-up message.
        self._keychain_usable_cache: bool | None = None
        # When file mode was entered by a real failure, the epoch after which to
        # re-probe the Keychain (see KEYCHAIN_RECHECK_COOLDOWN_S). 0.0 = no
        # pending re-probe (never failed, or forced to file mode deliberately).
        self._keychain_disabled_until: float = 0.0
        # Whether file mode was CHOSEN by us (a write fell back and we
        # deleted the Keychain item) rather than forced by a failed read. Both
        # stick, but only the failure means the file may be behind Claude
        # Code's own writes — see _read_active_credentials.
        self._file_mode_is_ours: bool = False
        # Whether any Keychain op has actually FAILED this process. Distinct
        # from _keychain_usable_cache, which is where ops should be ROUTED and
        # which _pin_file_mode sets deliberately: a routing choice must not
        # erase an observation. See _keychain_unreadable.
        self._keychain_op_failed: bool = False
        # Set by the ACTIVE OAuth read when THAT read could not reach the
        # Keychain. Per-item, not per-process: `_kc_call` clears
        # `_keychain_op_failed` on any success, and a backup read for an idle
        # slot goes straight through it — so one readable backup erased the
        # verdict recorded when the active read failed, and `degraded` flipped
        # False while the plaintext file still held the superseded generation.
        # "The Keychain answers" and "this active read succeeded" are different
        # facts; `degraded` needs the second.
        self._active_read_failed: bool = False
        # What _pin_file_mode OBSERVED about the residual active Keychain item.
        # None: never pinned. True: the delete returned, so nothing can shadow
        # the file. False: the delete could not run, so the file may be the
        # superseded generation. A fact about THIS item, which is why neither
        # flag above can stand in for it.
        self._residual_verdict: bool | None = None
        self._last_active_credentials_backend: str | None = None

    def _kc_call(self, fn, *args):
        """Run a ``macos_keychain`` wrapper call, learning Keychain usability.

        A success (including ``get_password`` returning ``None`` for a missing
        item) marks the Keychain usable — but only flips the cache ``None -> True``,
        never ``False -> True``: once a call has failed this run we stay in file
        mode so one invocation can't split-brain between backends. A
        ``KeychainError`` / ``TimeoutExpired`` / ``OSError`` (binary missing) marks
        it unusable and re-raises so the caller can fall back. Only those three are
        caught — a programming error propagates.

        Do NOT route ``item_exists`` through here: it returns ``False`` for both
        "absent" and "failed", so a timeout would be misread as a usable Keychain.
        """
        try:
            result = fn(*args)
        except macos_keychain.KEYCHAIN_ERRORS:
            # A Keychain op FAILED. Recorded separately from the capability
            # cache because that cache is a routing decision others overwrite —
            # `_pin_file_mode` clears it deliberately — while this is an
            # observation, and an observation cannot be undone by a later
            # choice. See `_keychain_unreadable`.
            self._keychain_op_failed = True
            self._keychain_usable_cache = False
            # Monotonic so a wall-clock jump can't expire the cooldown early/late.
            self._keychain_disabled_until = (
                time.monotonic() + KEYCHAIN_RECHECK_COOLDOWN_S
            )
            raise
        # A SUCCESS is an observation too, and it is the newer one. Recording
        # only failures made `_keychain_op_failed` monotone, and the cooldown
        # re-probe that normally masks a stale one is zeroed by
        # `_pin_file_mode` — so one transient timeout became permanent for the
        # process the moment any later write fell back. Measured: a read times
        # out, the Keychain recovers and the cooldown lapses (verified
        # `unreadable is False`), then a write pins and it is True forever,
        # with `degraded=True`, "keychain unavailable" on every usage pass, and
        # `cswap add` refused.
        #
        # Cleared unconditionally rather than only when the cache was None: the
        # cache is a ROUTING decision (which backend to use, deliberately
        # sticky), this is a FACT about whether the Keychain answers. A call
        # that just returned is proof it does.
        self._keychain_op_failed = False
        if self._keychain_usable_cache is None:
            self._keychain_usable_cache = True
        return result

    def _use_keychain(self) -> bool:
        """Whether credential ops should target the macOS Keychain right now.

        ``False`` off macOS. On macOS, ``True`` until a Keychain op fails, which
        drops to file mode. That failure records a re-probe deadline
        (``KEYCHAIN_RECHECK_COOLDOWN_S``): within one CLI invocation the deadline
        never passes, so a command can't split-brain between backends, but a
        long-running daemon re-probes once the cooldown elapses so a transient
        ``security`` timeout self-heals instead of sticking for the whole process.
        A pinned file mode with no deadline (0.0) stays sticky — see
        :meth:`_pin_file_mode` for why a write fallback must never re-probe.
        """
        if self._host.platform != Platform.MACOS:
            return False
        if (
            self._keychain_usable_cache is False
            and self._keychain_disabled_until
            and time.monotonic() >= self._keychain_disabled_until
        ):
            self._keychain_usable_cache = None  # cooldown elapsed → re-probe
            self._keychain_disabled_until = 0.0
        return self._keychain_usable_cache is not False

    def _pin_file_mode(self, *, residual_cleared: bool) -> None:
        """Pin file mode for the rest of the process — no Keychain re-probe.

        A read timeout is safe to recover from (re-probe on cooldown), but an
        active-credential *write* that falls back to the file is not: its
        best-effort delete of the old Keychain item may have failed, leaving a
        stale entry. Re-probing later could read that residual and show the wrong
        account, so once a write falls back we never re-probe onto a Keychain we
        could not verify-clear. Clears any re-probe deadline a prior read
        scheduled, which could otherwise still be pending.

        ``residual_cleared`` is the caller's OBSERVATION of that delete. True:
        nothing can shadow the file, so it genuinely is the authority, and the
        two failure flags are settled — whatever failed before cannot bear on a
        file nothing can shadow. False: a residual may survive, and that stays
        true however many unrelated Keychain items answer afterwards. Required,
        with no default — every call site has just run the delete, and a default
        is how an observation gets dropped in favour of a flag.

        A True verdict settles the PAST, not the future.
        """
        self._keychain_usable_cache = False
        self._keychain_disabled_until = 0.0
        self._file_mode_is_ours = True
        self._residual_verdict = residual_cleared
        if residual_cleared:
            # Settle what happened before; later failures are the flags'
            # question again. `_kc_call` re-arms the cooldown on any failure
            # with no pin check, and backup reads reach it without consulting
            # `_use_keychain`, so the active read IS reachable after a pin.
            # Measured: a stored True made a genuine later failure read
            # degraded=False and disarmed the capture guard.
            self._keychain_op_failed = False
            self._active_read_failed = False

    @property
    def _keychain_unreadable(self) -> bool:
        """The Keychain cannot be asked — so an empty read proves nothing.

        True only when a Keychain op FAILED. A file mode :meth:`_pin_file_mode`
        chose is excluded: nothing failed there, we wrote the credential to the
        file deliberately, and that file is the authority — an empty read means
        the slot really is empty.

        One predicate, because the two facts have been conflated once per site
        that spelled them out separately: every caller that means "unreadable"
        must ask here rather than read ``_keychain_usable_cache`` raw. The
        platform check belongs here for the same reason — off macOS there is no
        Keychain to be unreadable, and only every call path being macOS-gated
        keeps the cache at ``None`` there today.

        Asks through :meth:`_use_keychain` rather than the raw cache, because
        the cooldown re-probe lives there and the BACKUP read path never calls
        it: ``_read_account_credentials`` goes straight to ``_kc_read_backup``.
        Reading the flag raw made a single transient failure permanent for the
        process on exactly the paths that matter — a genuinely empty slot kept
        reporting "unreadable, do not re-add" (a dead end) and the consume gate
        kept deferring ``transient`` long after the Keychain answered again.
        A pinned file mode is unaffected: its deadline is 0.0, so
        ``_use_keychain`` never re-probes it.
        """
        if self._host.platform != Platform.MACOS:
            return False
        if self._use_keychain():          # may clear a lapsed cooldown
            return False
        # THE QUESTION IS WHETHER AN OP FAILED AND HAS NOT SINCE SUCCEEDED.
        #
        # It used to be `not self._file_mode_is_ours` — a pinned file mode read
        # as "nothing failed". On macOS that inverts the fact: the pin is
        # reachable ONLY THROUGH a Keychain op that just failed, since both
        # `_pin_file_mode` call sites are in the fallback branch of a write that
        # raised. Measured, identical world, one fallback write apart:
        #
        #     state A   degraded=True   sentinel='keychain unavailable'
        #     state B   degraded=False  sentinel='no credentials'
        #
        # `degraded=False` disarms `_refuse_degraded_capture`, and the sentinel
        # sends the user to re-add a slot whose backup is alive but unread. The
        # pin's own best-effort `_delete_active_keychain_entry()` failed too, so
        # a residual survives and Claude Code reads Keychain-first: our file is
        # the superseded generation, POSTed with the guard off.
        #
        # `_file_mode_is_ours` is NOT part of the answer any more, and saying so
        # matters: on macOS it is True only where `_keychain_op_failed` is also
        # True (measured across every production route to the pin — the OAuth
        # write fallback and the managed-key write fallback, with and without a
        # prior read failure), so `or not self._file_mode_is_ours` was a
        # disjunct that could never fire. Dropping it changes no test. It stays
        # in `_read_active_credentials`, where it answers its own question —
        # WHY we are in file mode — for the deliberate non-macOS and API-key
        # paths where nothing failed and the file really is the authority.
        return self._keychain_op_failed

    def _read_credentials(self) -> str | None:
        """Read Claude Code's active credential — OAuth *or* managed API key (value).

        Thin wrapper over :meth:`_read_active_credentials` preserving the historic
        ``str | None`` contract the switch paths rely on: credential string if
        found, ``""`` if not found, ``None`` on a file read error.
        """
        return self._read_active_credentials().value

    def _read_active_oauth_keychain(self) -> tuple[str | None, bool]:
        """Read the active profile's OAuth Keychain item(s).

        Reads the item(s) :func:`_active_oauth_keychain_services` resolves for
        the current environment, in order, stopping at the first hit. Only the
        explicit-``CLAUDE_CONFIG_DIR``-names-the-default-profile case yields
        more than one; see that function for why.

        Returns ``(value, failed)`` exactly as before: ``value`` is the
        credential string, or ``None`` when every item was absent (rc-44) or the
        Keychain was unreadable. ``failed`` is True only for unreadable.

        An unreadable Keychain stops the walk. It is a property of the Keychain,
        not of the item, so a second service name cannot fare better — and
        ``_kc_call`` has already flipped routing to file mode by then.
        """
        for service in _active_oauth_keychain_services():
            value, failed = self._read_one_oauth_keychain(service)
            if value:
                return value, False
            if failed:
                return None, True
        return None, False

    def _read_one_oauth_keychain(self, service: str) -> tuple[str | None, bool]:
        """Read one OAuth Keychain item with a bounded retry.

        Returns ``(value, failed)``. ``value`` is the credential string, or
        ``None`` when the item is absent (rc-44) or unreadable. ``failed`` is True
        only when *every* attempt raised a KeychainError (locked / denied /
        timeout); a genuinely absent item (rc-44, returned as ``None`` without
        raising) reports ``failed=False`` and is not retried. The retry rides out
        a transient lock/contention — it does not paper over an internal race.
        """
        last_error: Exception | None = None
        for attempt in range(_ACTIVE_READ_ATTEMPTS):
            try:
                value = self._kc_call(
                    macos_keychain.get_password,
                    service,
                    macos_keychain.keychain_account_name(),
                )
                return value, False
            except macos_keychain.KEYCHAIN_ERRORS as e:
                last_error = e
                if attempt + 1 < _ACTIVE_READ_ATTEMPTS:
                    time.sleep(_ACTIVE_READ_RETRY_DELAY)
        # Every attempt failed: _kc_call has flipped routing to file mode.
        self._host._logger.warning(
            f"Keychain read failed after {_ACTIVE_READ_ATTEMPTS} attempt(s), "
            f"trying file: {last_error}"
        )
        return None, True

    def _read_active_credentials(self) -> ActiveCredentials:
        """Read Claude Code's active credential, classifying the outcome.

        Tries the OAuth credential first (Keychain "Claude Code-credentials" on
        macOS when usable — with a bounded retry to ride out a transient
        lock/contention — then the plaintext ``~/.claude/.credentials.json`` Claude
        Code also falls back to), and only then the managed-key locations (macOS
        Keychain "Claude Code", then ``~/.claude.json`` ``primaryApiKey``). Trying
        OAuth fully first means a macOS OAuth login that only has a file fallback
        (Keychain empty) is never misread as an API key. A returned managed key is a
        raw ``sk-ant-api…`` string — callers distinguish it via ``looks_like_api_key``.
        Non-mutating.

        Reports ``keychain_unavailable`` when the OAuth Keychain read failed and
        nothing else covered it, so the display layer can say "keychain unavailable"
        rather than "no credentials" for a merely-unreadable slot — which would
        otherwise nudge the user into an unnecessary re-login.
        """
        keychain_failed = False
        # 1. OAuth Keychain (macOS, when usable), with a bounded retry.
        #
        # READ THE ITEM FOR *THIS* PROFILE, NOT THE FIXED NAME.
        # CLAUDE_CODE_KEYCHAIN_SERVICE is a fixed name, but Claude Code stores
        # only the DEFAULT profile's credential under it: with CLAUDE_CONFIG_DIR
        # set it scopes the item to that config dir under a hashed service name.
        # Reading the fixed name from a custom profile therefore returns a
        # credential belonging to a different account, while the identity read
        # one layer up (paths.get_claude_config_home, which does honor
        # CLAUDE_CONFIG_DIR) reports the custom profile's. Pairing an identity
        # with another account's token is silent, and every consumer of this
        # read inherits it.
        #
        # REDIRECTED, NOT SKIPPED. Skipping the keychain under a custom profile
        # would leave macOS mostly blind: claude writes rotations keychain-only
        # there, so the step-2 plaintext file frequently does not exist at all
        # and a logged-in profile would render as "no credentials" — trading a
        # wrong answer for a missing one. The hashed name is not a guess in this
        # codebase either: session.keychain_service_name codifies claude's
        # derivation (pinned against its source) and the delete, session-read
        # and capture paths already depend on it.
        if self._use_keychain():
            val, keychain_failed = self._read_active_oauth_keychain()
            # THIS read's own verdict, kept so a later success on some OTHER
            # item cannot erase it. Sticky until this read succeeds again,
            # which is what makes it self-heal without being erasable.
            self._active_read_failed = keychain_failed
            if val:
                return ActiveCredentials(val, False)
        elif self._residual_verdict is False or (
            self._active_read_failed or self._keychain_unreadable
        ):
            # Keychain already known unusable this process: if nothing is found
            # below, that absence is "keychain unavailable", not an empty slot.
            # An UNVERIFIED clear says so on its own — no later success on some
            # other item speaks for this one. A verified clear does not appear
            # here at all: it settled the flags at the pin, so anything they
            # say now happened AFTER it.
            keychain_failed = True

        # 2. OAuth plaintext file (Claude Code's own fallback; every platform).
        # After a FAILED keychain read this file may hold a stale generation
        # (CC writes rotations keychain-only on macOS) — the flag travels as
        # ``degraded`` so consume paths refuse these bytes.
        cred_file = get_credentials_path()
        if cred_file.exists():
            try:
                text = cred_file.read_text(encoding="utf-8")
            except Exception as e:
                self._host._logger.error(f"Failed to read credentials file: {e}")
                # `keychain_unavailable` is NOT False here. Keychain denied AND
                # the fallback file unreadable is the most unreadable state
                # there is; hardcoding False made it render as "no credentials"
                # and sent the user to a re-login that cannot help.
                return ActiveCredentials(None, keychain_failed, keychain_failed)
            if text.strip():
                return ActiveCredentials(text, False, keychain_failed)

        # 3. Managed API key (Keychain "Claude Code" on macOS, then primaryApiKey).
        key = self._read_managed_key()
        if key:
            return ActiveCredentials(key, False, keychain_failed)
        # Nothing anywhere. Flag a failed-and-uncovered OAuth Keychain read so the
        # UI distinguishes it from a real empty slot.
        return ActiveCredentials("", keychain_failed, keychain_failed)

    def _read_managed_key(self) -> str:
        """Read the active managed API key, or "" when absent. Non-mutating.

        macOS Keychain "Claude Code" (when usable) first, then ``~/.claude.json``
        ``primaryApiKey`` — mirroring Claude Code's
        ``getApiKeyFromConfigOrMacOSKeychain``.

        The Keychain half is default-profile-only. Unlike the OAuth item above
        this one is gated rather than redirected, because there is no codified
        derivation to redirect it *to*: ``session.keychain_service_name`` covers
        the credentials item, and claude's managed-key service name under a
        custom profile is not pinned anywhere in this repo. Guessing it is the
        thing the OAuth half can avoid and this half cannot.

        Gating matches what capture already does.
        ``_read_capture_credentials`` ends on "only this profile's own
        ``primaryApiKey`` — never the unsuffixed 'Claude Code' Keychain item,
        which belongs to the default profile and would answer for a login that
        is not the one being added". Same item, same conclusion; this makes the
        read side agree with the capture side instead of contradicting it.

        ``primaryApiKey`` below is read from the active profile's own config and
        stays, so a custom profile with a managed key is still found.
        """
        if _active_profile_is_default() and self._use_keychain():
            try:
                val = self._kc_call(
                    macos_keychain.get_password,
                    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
                    macos_keychain.keychain_account_name(),
                )
            except macos_keychain.KEYCHAIN_ERRORS as e:
                self._host._logger.warning(f"Managed-key Keychain read failed: {e}")
                val = None
            if val:
                return val
        cfg = self._read_global_config()
        if cfg:
            key = cfg.get("primaryApiKey")
            if isinstance(key, str) and key:
                return key
        return ""

    def _read_global_config(self) -> dict | None:
        """Read and parse ``~/.claude.json``, or None when absent/unreadable."""
        path = get_global_config_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            self._host._logger.warning(f"Failed to read global config: {e}")
            return None
        return data if isinstance(data, dict) else None

    def _update_global_config(self, mutator) -> None:
        """Atomically apply ``mutator(dict)`` to ``~/.claude.json``, key-scoped.

        Reads the current config, lets ``mutator`` change only the keys it owns
        (``primaryApiKey`` / ``customApiKeyResponses``), and writes it back
        atomically — preserving every other key (``oauthAccount``, projects,
        settings). 0o600 mirrors the switcher's ``_write_json``.
        """
        path = get_global_config_path()
        try:
            data = self._read_global_config()
        except Exception as e:  # pragma: no cover - defensive
            raise CredentialWriteError(f"Failed to read global config for update: {e}")
        if data is None and path.exists():
            # UNREADABLE, not absent — the same distinction `_clear_managed_key`
            # makes, on the file carrying the user's whole Claude Code state.
            # `or {}` collapsed the two and the atomic replace then wrote that
            # `{}` over a file it had never read. Measured against a torn config
            # (valid prefix, truncated tail — what a crash mid-write leaves):
            # `oauthAccount`, `projects` and `mcpServers` were gone.
            # An ABSENT config has nothing to preserve and is a genuine start.
            raise CredentialWriteError(
                f"{path} exists but could not be read — refusing to overwrite "
                "it. Move or repair the file, then retry."
            )
        data = data or {}
        mutator(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
            os.close(fd)
            fd = -1
            replace_with_retry(tmp_path, str(path))
            if sys.platform != "win32":
                os.chmod(str(path), 0o600)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _write_active_credentials_file(self, credentials: str) -> None:
        """Atomically write Claude Code's plaintext active-credentials file."""
        cred_dir = get_claude_config_home()
        cred_dir.mkdir(parents=True, exist_ok=True)
        cred_file = cred_dir / ".credentials.json"
        import tempfile
        fd, tmp_path = tempfile.mkstemp(dir=str(cred_dir), suffix=".tmp")
        try:
            os.write(fd, credentials.encode("utf-8"))
            os.close(fd)
            fd = -1
            replace_with_retry(tmp_path, str(cred_file))
            if sys.platform != "win32":
                os.chmod(str(cred_file), 0o600)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _delete_active_keychain_entry(self) -> bool:
        """Best-effort removal of the active-credential Keychain item (macOS only).

        Claude Code reads the Keychain before the plaintext file, so once we fall
        back to the file we must clear any stale Keychain entry or Claude Code would
        resurrect it (#30337). Best-effort: when the Keychain is down the delete
        can't run, which is the documented recovery residual.

        Returns whether no active item can shadow the file. ``delete_password``
        returns only on rc 0 or rc 44 (already absent) and raises otherwise, so
        a return is proof — which is the fact ``_pin_file_mode`` needs and used
        to discard. Off macOS there is no Keychain item, hence ``True``.
        """
        if self._host.platform != Platform.MACOS:
            return True
        try:
            macos_keychain.delete_password(
                CLAUDE_CODE_KEYCHAIN_SERVICE, macos_keychain.keychain_account_name()
            )
        except Exception:
            return False  # best-effort; a down Keychain can't be cleaned now
        return True

    def _write_credentials(self, credentials: str) -> None:
        """Write Claude Code's active credential, enforcing a single auth axis.

        Detects the kind from the payload (raw ``sk-ant-api…`` key vs OAuth JSON) and
        mirrors Claude Code's own ``saveApiKey``/``removeApiKey``: activating one axis
        clears the other so a stale credential can't shadow the switch.

        - **OAuth** → write the OAuth credential (see ``_write_oauth_credentials``),
          then clear any managed key (Keychain "Claude Code" + ``primaryApiKey``;
          ``approved`` left intact, as ``removeApiKey`` does).
        - **API key** → record ``key[-20:]`` in ``approved`` and store the key (macOS
          Keychain "Claude Code" when usable, else ``~/.claude.json`` ``primaryApiKey``),
          then clear the OAuth credential (Keychain item + ``.credentials.json``).

        Raises:
            CredentialWriteError: If writing credentials fails.
        """
        if looks_like_api_key(credentials):
            self._write_managed_credentials(credentials.strip())
        else:
            self._write_oauth_credentials(credentials)
            self._clear_managed_key()

    def _write_managed_credentials(self, api_key: str) -> None:
        """Activate a managed API key, then clear OAuth (mutual exclusion).

        Always records ``key[-20:]`` in ``customApiKeyResponses.approved`` (Claude
        Code does this on every platform, even on Keychain success — otherwise it
        re-prompts to approve the key). Stores the key in the macOS Keychain when
        usable, else ``~/.claude.json`` ``primaryApiKey`` (matching ``saveApiKey``'s
        keychain-then-config fallback). Finally clears the OAuth credential.

        Raises:
            CredentialWriteError: If persisting the key fails.
        """
        wrote_to_keychain = False
        if self._use_keychain():
            try:
                self._kc_call(
                    macos_keychain.set_password,
                    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
                    macos_keychain.keychain_account_name(),
                    api_key,
                )
            except macos_keychain.KEYCHAIN_ERRORS as e:
                # _kc_call flipped routing to file mode; fall back to config below.
                self._host._logger.warning(
                    f"Managed-key Keychain write failed, falling back to config: {e}"
                )
            else:
                wrote_to_keychain = True

        approved = approved_form(api_key)

        def _mutate(cfg: dict) -> None:
            responses = cfg.get("customApiKeyResponses")
            if not isinstance(responses, dict):
                responses = {}
            approved_list = responses.get("approved")
            if not isinstance(approved_list, list):
                approved_list = []
            if approved not in approved_list:
                approved_list.append(approved)
            responses["approved"] = approved_list
            responses.setdefault("rejected", [])
            cfg["customApiKeyResponses"] = responses
            if wrote_to_keychain:
                # Keychain holds the key; keep it out of plaintext config.
                cfg.pop("primaryApiKey", None)
            else:
                cfg["primaryApiKey"] = api_key

        try:
            self._update_global_config(_mutate)
        except CredentialWriteError:
            raise
        except Exception as e:
            raise CredentialWriteError(f"Failed to write managed API key: {e}")

        # Mutual exclusion: drop the OAuth credential so it can't shadow the key.
        self._clear_oauth_credential()
        if self._host.platform == Platform.MACOS and not wrote_to_keychain:
            # Same stale-Keychain resurrection guard as the OAuth path: the key
            # fell back to plaintext ``primaryApiKey`` while a stale "Claude Code"
            # Keychain item may remain, and managed-key reads check the Keychain
            # before ``primaryApiKey``. Pin file mode so a cooldown re-probe can't
            # read that residual over the fresh fallback value.
            #
            # ``residual_cleared=False``: the item that would shadow here is the
            # MANAGED one, and nothing deleted it — ``_clear_oauth_credential``
            # above removes the OAuth item, a different service. Unverified, so
            # the conservative verdict is the true one.
            self._pin_file_mode(residual_cleared=False)
        self._last_active_credentials_backend = (
            "keychain" if wrote_to_keychain else "file"
        )

    def _clear_managed_key(self) -> None:
        """Clear any active managed API key (Claude Code ``removeApiKey`` semantics).

        Deletes the macOS Keychain "Claude Code" item (best-effort) and drops
        ``primaryApiKey`` from ``~/.claude.json``. Leaves
        ``customApiKeyResponses.approved`` untouched — ``removeApiKey`` doesn't clear
        it either, and removing it would force recovering ``key[-20:]`` from the
        Keychain for no benefit. A no-op (no config rewrite) when no key is present.

        I-2 (round 9): ``_read_global_config`` collapses ABSENT and UNREADABLE
        into the same ``None`` — without the distinction below, an unreadable
        config (permissions, mid-unmount) reads exactly like a genuinely
        keyless profile, so the clear is silently skipped. Best-effort stays
        best-effort here (never raises — the write path this feeds must not
        block on a transient read glitch), but a distinguishing warning
        matters: a stale ``primaryApiKey`` surviving alongside a freshly
        activated OAuth credential is a live cross-account key that bills
        per token while it lies, and a caller/log reader must be able to
        tell "nothing to clear" from "could not check".
        """
        if self._host.platform == Platform.MACOS:
            try:
                macos_keychain.delete_password(
                    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
                    macos_keychain.keychain_account_name(),
                )
            except Exception:
                pass  # best-effort; a down Keychain can't be cleaned now
        cfg = self._read_global_config()
        if cfg is None and get_global_config_path().exists():
            self._host._logger.warning(
                "Could not clear primaryApiKey: the global config exists "
                "but could not be read (unreadable, not absent) — leaving "
                "it in place rather than overwriting it unread"
            )
            return
        if cfg is not None and cfg.get("primaryApiKey") is not None:
            def _drop(c: dict) -> None:
                c.pop("primaryApiKey", None)

            try:
                self._update_global_config(_drop)
            except Exception as e:
                self._host._logger.warning(f"Failed to clear primaryApiKey: {e}")

    def _clear_oauth_credential(self) -> None:
        """Clear the active OAuth credential — Keychain item and plaintext file.

        Best-effort: a down Keychain or missing file is fine. Removing
        ``.credentials.json`` stops Claude Code from falling back to a stale OAuth
        login over the just-activated API key.
        """
        self._delete_active_keychain_entry()
        cred_file = get_credentials_path()
        try:
            if cred_file.exists():
                cred_file.unlink()
        except OSError as e:
            self._host._logger.warning(f"Failed to remove credentials file: {e}")

    def _write_oauth_credentials(self, credentials: str) -> None:
        """Write Claude Code's active OAuth credentials.

        macOS writes the Keychain when usable (recording backend ``"keychain"``). On
        a successful Keychain write it then **rewrites an already-present**
        ``.credentials.json`` with the same fresh creds — never *creating* one when
        absent, never *deleting* one. This bumps the file's mtime so a running Claude
        Code session's disk-mtime cache invalidation fires and it hot-reloads the new
        account instead of serving its memoized token until restart (#86); it also
        keeps the file consistent for the container ``~/.claude`` sharing consumer
        (#1414) rather than stranding it on stale content. Keychain-only users keep
        their fileless posture — their absent-file path already hot-reloads via the
        ~30s Keychain TTL — and never gain a plaintext credential on disk. If the
        Keychain write fails — or the Keychain is already known unusable — it writes
        the plaintext file and best-effort clears any stale Keychain entry (#30337),
        recording backend ``"file"``. Linux/WSL/Windows always write the file.

        Raises:
            CredentialWriteError: If writing credentials fails.
        """
        if self._use_keychain():
            try:
                self._kc_call(
                    macos_keychain.set_password,
                    CLAUDE_CODE_KEYCHAIN_SERVICE,
                    macos_keychain.keychain_account_name(),
                    credentials,
                )
            except macos_keychain.KEYCHAIN_ERRORS as e:
                # _kc_call flipped routing to file mode; fall through to the file.
                # (A programming error is NOT caught here — it propagates.)
                self._host._logger.warning(f"Keychain write failed, falling back to file: {e}")
            else:
                # Keychain (primary) now holds the fresh credential. Bump an
                # already-present shadow file's mtime so running sessions hot-reload
                # (#86); best-effort, never creates one — see the helper.
                self._refresh_stale_credentials_file(credentials)
                self._last_active_credentials_backend = "keychain"
                return

        # File mode: non-macOS, macOS Keychain known unusable, or a Keychain write
        # that just failed. Write the plaintext file and (macOS) best-effort clear
        # any stale Keychain entry so Claude Code's keychain-first read can't shadow
        # it (#30337).
        try:
            self._write_active_credentials_file(credentials)
        except Exception as e:
            raise CredentialWriteError(f"Failed to write credentials: {e}")
        cleared = self._delete_active_keychain_entry()
        if self._host.platform == Platform.MACOS:
            # The delete above is best-effort; a stale Keychain item may remain.
            # Pin file mode so a later read-timeout cooldown can't re-probe onto
            # that residual and resurrect the wrong account (see _pin_file_mode).
            # Its outcome is also the answer to whether the file is now the
            # authority, so hand it over rather than re-deriving it from flags.
            self._pin_file_mode(residual_cleared=cleared)
        self._last_active_credentials_backend = "file"

    def _refresh_stale_credentials_file(self, credentials: str) -> None:
        """Bump an already-present ``.credentials.json``'s mtime after a Keychain write.

        Rewrite-when-present / never-create (#86). Claude Code invalidates its
        memoized OAuth token only when this file's mtime changes or the file is
        absent; a Keychain-only switch leaves a *stale* file's mtime frozen, so a
        running session serves the old token until restart. Rewriting the existing
        file with the same fresh creds bumps the mtime (atomic ``os.replace``, so it
        bumps even when the content is unchanged) and keeps a file-reading consumer
        (#1414 shared ``~/.claude``) consistent. We never *create* the file when
        absent — Keychain-only users keep their fileless posture and their absent-file
        (~30s Keychain-TTL) path already hot-reloads.

        Best-effort: the Keychain write is authoritative on macOS and already
        succeeded, so a failure here must not fail the switch — it only means a
        running session may lag until restart.
        """
        cred_file = get_credentials_path()
        if not cred_file.exists():
            return
        try:
            self._write_active_credentials_file(credentials)
        except Exception as e:
            self._host._logger.warning(
                f"Could not refresh .credentials.json after Keychain write ({e}); "
                "a running session may not hot-reload until restart"
            )

    def _uses_file_backup_backend(self) -> bool:
        """Whether per-account backup *writes* go to files vs. the Keychain.

        Linux/WSL/Windows always use base64 ``.enc`` files under ``credentials_dir``
        (Windows moved off the Credential Manager because it rejects entries over
        ~2,500 bytes, #45). macOS uses the Keychain while it's usable and falls back
        to ``.enc`` files when it isn't (headless/SSH/locked); UNKNOWN platforms have
        no Keychain, so they use files too. Backup *reads* are ``.enc``-wins
        regardless (see ``_read_account_credentials``).
        """
        return not self._use_keychain()

    # -- backup credential backends ---------------------------------------
    #
    # Two backends for per-account backups: base64 ``.enc`` files under
    # ``credentials_dir`` and the macOS Keychain (``SECURITY_SERVICE``). On macOS
    # reads are ``.enc``-wins: a fallback ``.enc`` (written while the Keychain was
    # unusable) is authoritative over a possibly-stale Keychain copy, so a Keychain
    # that recovers can't shadow a newer file. A successful Keychain write
    # therefore reconciles the ``.enc`` away (correctness-critical, not best-effort).

    def _backup_enc_path(self, account_num: str, email: str) -> Path:
        return self._host.credentials_dir / f".creds-{account_num}-{email}.enc"

    def _backup_username(self, account_num: str, email: str) -> str:
        return f"account-{account_num}-{email}"

    def _kc_read_backup(self, account_num: str, email: str) -> str:
        """Read a per-account backup from the Keychain only (no file fallback).

        Routes through ``_kc_call`` (so a failure flips the capability cache).
        Returns ``""`` when the item is absent; raises on a Keychain failure so
        the caller decides (normal reads swallow it; the migration defers).
        """
        creds = self._kc_call(
            macos_keychain.get_password,
            SECURITY_SERVICE,
            self._backup_username(account_num, email),
        )
        return creds or ""

    def _kc_write_backup(self, account_num: str, email: str, credentials: str) -> None:
        """Write a per-account backup to the Keychain only. Raises on failure."""
        self._kc_call(
            macos_keychain.set_password,
            SECURITY_SERVICE,
            self._backup_username(account_num, email),
            credentials,
        )

    def _kc_delete_backup(self, account_num: str, email: str) -> None:
        """Delete a per-account backup Keychain item only. Raises on failure."""
        self._kc_call(
            macos_keychain.delete_password,
            SECURITY_SERVICE,
            self._backup_username(account_num, email),
        )

    def _kc_delete_backup_prev(self, account_num: str, email: str) -> None:
        """Delete a slot's retained ``.prev`` Keychain item. Raises on failure."""
        self._kc_call(
            macos_keychain.delete_password,
            SECURITY_SERVICE,
            self._prev_backup_username(account_num, email),
        )

    def _delete_backup_keychain_quiet(self, account_num: str, email: str) -> None:
        """Best-effort backup Keychain delete (never raises)."""
        try:
            self._kc_delete_backup(account_num, email)
        except Exception as e:
            self._host._logger.warning(f"Failed to delete credentials from Keychain: {e}")

    def _write_backup_enc(self, account_num: str, email: str, credentials: str) -> None:
        """Atomically write a per-account backup ``.enc`` (base64) file."""
        self._atomic_b64_write(self._backup_enc_path(account_num, email), credentials)

    def _atomic_b64_write(self, target: Path, credentials: str) -> None:
        """Atomically write a base64-encoded credential file (0600)."""
        self._host.credentials_dir.mkdir(parents=True, exist_ok=True)
        enc_file = target
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
        import tempfile
        fd, tmp_path = tempfile.mkstemp(dir=str(self._host.credentials_dir), suffix=".tmp")
        try:
            os.write(fd, encoded.encode("utf-8"))
            os.close(fd)
            fd = -1
            replace_with_retry(tmp_path, str(enc_file))
            if sys.platform != "win32":
                os.chmod(str(enc_file), 0o600)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _reconcile_enc_after_keychain_write(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Stop a leftover ``.enc`` from shadowing a just-written Keychain backup.

        ``.enc``-wins reads make this correctness-critical: delete the ``.enc``; if
        the delete fails, atomically rewrite it with the same fresh creds; if that
        also fails, raise so the inconsistency surfaces rather than serving stale.
        """
        enc_file = self._backup_enc_path(account_num, email)
        if not enc_file.exists():
            return
        try:
            enc_file.unlink()
            return
        except Exception as e:
            self._host._logger.warning(
                f"Could not delete .enc after Keychain backup write ({e}); "
                "rewriting it with the fresh credentials to keep both consistent"
            )
        self._write_backup_enc(account_num, email, credentials)

    def _read_account_credentials(
        self, account_num: str, email: str, failed: list | None = None
    ) -> str:
        """Read account credentials from backup. ``""`` when missing.

        macOS is ``.enc``-wins (a fallback file beats a possibly-stale Keychain
        copy); only an absent or corrupt ``.enc`` falls through to the Keychain.
        Linux/WSL/Windows read the ``.enc`` only.
        """
        enc_file = self._backup_enc_path(account_num, email)
        try:
            # `stat`, NOT `exists`. `Path.exists()` SWALLOWS OSError from 3.13
            # on and answers False, so an unsearchable credentials/ dir became
            # byte-identical to a genuinely absent backup — on 3.12 the raise
            # reached the handler below and marked the read failed, on 3.13+
            # nothing did. The platform decided whether a read failure was
            # reported, and the fleet runs both (3.12 on two machines, 3.14 on
            # one). `stat` raises on every version, so the distinction the
            # handler below exists to draw survives on all of them.
            enc_file.stat()
        except FileNotFoundError:
            enc_present = False
        except OSError as e:
            # The directory itself could not be searched (permissions, a
            # mid-unmount, ...) — a real read failure, same as the arm below
            # that fires once the file is known to exist. Not marking
            # `failed` here makes an unsearchable dir byte-identical to a
            # genuinely absent backup, which is exactly what C1 fixed for
            # the file itself six lines below.
            if failed is not None:
                failed.append(True)
            self._host._logger.warning(f"Failed to read credentials file: {e}")
            enc_present = False
        else:
            enc_present = True
        if enc_present:
            try:
                encoded = enc_file.read_text(encoding="utf-8").strip()
            except OSError as e:
                # The .enc EXISTS but could not be read (permissions, a
                # mid-unmount, ...) — a real read failure, not "no backup".
                # Reported through the caller's list, the same as the
                # Keychain OSError arm below: this is the ONLY backend on
                # Linux/WSL/Windows, and it wins over the Keychain on macOS,
                # so masking it must not read as "absent" on any platform.
                if failed is not None:
                    failed.append(True)
                self._host._logger.warning(f"Failed to read credentials file: {e}")
            else:
                try:
                    # validate=True: reject non-alphabet junk (e.g. "!!!!") instead
                    # of silently discarding it to empty bytes, which would let a
                    # corrupt .enc shadow a valid Keychain copy.
                    decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
                except Exception as e:
                    # Corrupt/garbled .enc → on macOS fall through to the Keychain
                    # copy (documented recovery) — content-level, not a read
                    # failure, so this does NOT mark `failed`.
                    self._host._logger.warning(f"Failed to read credentials file: {e}")
                else:
                    if decoded:
                        return decoded
                    # Empty/whitespace .enc is not a real backup → try the Keychain.
        if self._host.platform == Platform.MACOS:
            try:
                return self._kc_read_backup(account_num, email)
            except macos_keychain.KEYCHAIN_ERRORS as e:
                # Reported through the caller's list, not an instance flag:
                # that flag was shared by every thread and its window spans a
                # ~10-50ms `security` subprocess. Measured with one sibling
                # reader, slot 2 genuinely denied: 2 of 60 reads came back
                # "readable", and the consume gate then POSTs a spent grant.
                if failed is not None:
                    failed.append(True)
                self._host._logger.warning(f"Failed to read credentials from Keychain: {e}")
        return ""

    def _read_account_credentials_ex(
        self, account_num: str, email: str
    ) -> tuple[str, bool]:
        """Backup read with an unreadable-vs-absent verdict.

        Returns ``(value, unreadable)``. ``unreadable`` is True when the read
        that produced ``""`` actually FAILED rather than finding nothing: the
        ``.enc`` exists but raised an ``OSError`` (permissions, a mid-unmount
        — every platform), or the ``.enc`` had nothing and the macOS Keychain
        read itself *raised* (locked / denied / timeout). A genuinely absent
        backup (no .enc, keychain answered "not found") reads as
        ``("", False)``. Callers use the distinction to say "keychain
        unavailable — retry from a GUI session" instead of nudging the user
        into an unnecessary re-add/re-login. The ``.enc`` is the ONLY backend
        on Linux/WSL/Windows, so its own read failure must reach this verdict
        there too, not only on macOS.
        """
        failed: list = []
        value = self._read_account_credentials(account_num, email, failed)
        if value:
            return value, False
        if self._host.platform != Platform.MACOS:
            return "", bool(failed)
        # Whether THIS read reached the Keychain, observed at the read itself.
        #
        # This used to ask `_keychain_unreadable`, which answers a different
        # question: has any Keychain op failed and not been superseded. It gets
        # overwritten by an unrelated event. `_pin_file_mode` sets
        # `_file_mode_is_ours` and clears the re-probe deadline, and on macOS it
        # is only reachable THROUGH a Keychain op that just failed — both call
        # sites are in the fallback branch of a write that raised. So one
        # active-credential write fallback flipped this answer for the rest of
        # the process while the Keychain stayed locked:
        #
        #     state A   _read_account_credentials_ex('3') -> ('', True)
        #     ...one fallback write...
        #     state B   _read_account_credentials_ex('3') -> ('', False)
        #
        # The pin's premise ("we wrote the credential to the file, that file is
        # the authority") holds for the ACTIVE credential and says nothing about
        # the BACKUP, which is Keychain-only by design —
        # `_reconcile_enc_after_keychain_write` deletes the `.enc` after a
        # successful Keychain write. So the consume gate read "genuinely empty",
        # POSTed the caller's possibly-superseded snapshot, took invalid_grant,
        # and quarantined a slot whose live refresh token was sitting unread.
        #
        # Cleared above rather than trusted from a previous call: a caller that
        # substitutes `_read_account_credentials` never reaches the setter, and
        # a stale True there would defer forever.
        #
        # Swapping this for `_keychain_unreadable` was once believed to leave
        # the suite green on a single thread (the process flag supposedly
        # agreeing with `_kc_read_backup`'s own `_kc_call`-routed verdict on
        # every state one thread can reach) — re-measured directly (swap the
        # return value, run the full suite) and that does NOT reproduce:
        # `TestOurOwnFileModeIsNotAKeychainFailure::
        # test_two_concurrent_backup_reads_keep_their_own_verdicts` and
        # `TestEncPermissionDeniedIsUnreadable::
        # test_unreadable_enc_is_not_absent_on_macos` both fail on a single
        # thread. Concurrently it is wrong for the additional reason above
        # (the sentinel calls this seam unlocked while the consume gate holds
        # a per-slot lock, and the TUI runs three workers on one store) — but
        # the single-thread case was never actually safe either.
        return "", bool(failed)

    def _write_account_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Write account credentials to backup (pure I/O — no session invalidation).

        macOS writes the Keychain when usable, then reconciles the ``.enc`` away
        (see ``_reconcile_enc_after_keychain_write``). When the Keychain is unusable
        it writes the ``.enc`` atomically, then best-effort deletes any stale
        Keychain copy so a recovered Keychain can't shadow the fresh file.
        Linux/WSL/Windows write the ``.enc`` only.

        Raises on a file-write failure **before** returning, so the switcher wrapper
        runs ``_post_backup_write`` exactly once and only after a successful write.

        Before overwriting, the current generation is retained as a ``.prev`` file
        (one generation, best-effort): a refresh token exists in exactly one place,
        giving a misclassified overwrite a best-effort chance of recovery without
        a /login.
        """
        self._retain_previous_backup(account_num, email, credentials)
        if self._use_keychain():
            try:
                self._kc_write_backup(account_num, email, credentials)
            except macos_keychain.KEYCHAIN_ERRORS as e:
                # Keychain unusable; _kc_call flipped routing to file mode.
                # (A programming error is NOT caught here — it propagates.)
                self._host._logger.warning(
                    f"Keychain backup write failed, falling back to file: {e}"
                )
            else:
                self._reconcile_enc_after_keychain_write(account_num, email, credentials)
                return

        # File mode: write the .enc atomically, then (macOS) best-effort drop the
        # stale Keychain copy so a recovered Keychain can't shadow the fresh file.
        try:
            self._write_backup_enc(account_num, email, credentials)
        except Exception as e:
            self._host._logger.warning(f"Failed to write credentials file: {e}")
            raise
        if self._host.platform == Platform.MACOS:
            self._delete_backup_keychain_quiet(account_num, email)

    def _delete_account_credentials(self, account_num: str, email: str) -> None:
        """Delete account credentials from backup (both backends on macOS).

        Removes the ``.enc`` file(s) and, on macOS, the Keychain item(s). The
        Keychain delete is best-effort: if it's locked the item may linger as
        harmless unreferenced cruft (the slot is gone from sequence.json; a re-add
        overwrites it via ``-U``; purge sweeps it). Includes the legacy
        ``account-None-{email}`` alias.
        """
        nums = [account_num]
        if str(account_num) != "None":
            nums.append("None")
        for num in nums:
            enc_file = self._backup_enc_path(num, email)
            try:
                if enc_file.exists():
                    enc_file.unlink()
            except Exception as e:
                self._host._logger.warning(f"Failed to delete credentials file: {e}")
            if self._host.platform == Platform.MACOS:
                self._delete_backup_keychain_quiet(num, email)
            self.delete_previous_backup(num, email)

    def delete_account_credentials_strict(
        self, account_num: str, email: str
    ) -> None:
        """Clear a slot key, failing closed: raise unless emptiness is assured.

        For transactional pre-commit clears (the swap/move write-or-clear
        step and rollback restoration): a destination that must be empty but
        may still serve material is exactly the wrong-credential state the
        transaction exists to prevent, so backend failures must abort the
        commit rather than be logged away. A read-back alone cannot provide
        this: the normal reader converts Keychain errors to ``""``, which
        conflates "absent" with "unreadable" — a locked Keychain holding a
        stale item would pass verification and resurface on unlock. So the
        served backends are deleted with errors propagating; absence itself
        counts as success on both (missing ``.enc``; Keychain rc 44). The
        Keychain delete runs even when routing says file mode, for the same
        reason. Legacy-alias and ``.prev`` sweeps stay best-effort — reads
        never serve them. The best-effort variant remains right for
        post-commit cleanup, where a failure only leaks an unreferenced file.
        """
        # Best-effort sweep first: same cruft cleanup (legacy alias, .prev,
        # quiet Keychain) a normal delete performs.
        self._delete_account_credentials(account_num, email)
        # Then assure the served key really is gone, propagating failures.
        # Unconditional unlink: exists() returns False on an inaccessible
        # directory, which would fail open here — missing is fine
        # (missing_ok), permission/I/O errors must abort the commit.
        try:
            self._backup_enc_path(account_num, email).unlink(missing_ok=True)
            if self._host.platform == Platform.MACOS:
                self._kc_delete_backup(account_num, email)
        except (OSError, *macos_keychain.KEYCHAIN_ERRORS) as e:
            raise CredentialError(
                f"Could not clear stored credentials for slot {account_num} "
                f"({email}) — aborting before commit: {e}"
            ) from e
        # Final belt: catches any backend view the deletes above missed. The
        # plain reader cannot serve this — it is the exact reader this
        # docstring says "conflates absent with unreadable" — so an
        # unreadable-but-present view (a locked Keychain, a permission
        # glitch on the .enc) would pass verification and resurface later.
        # `_ex` distinguishes the two; either a served value OR an
        # unreadable verdict aborts the commit.
        value, unreadable = self._read_account_credentials_ex(account_num, email)
        if value or unreadable:
            raise CredentialError(
                f"Could not clear stored credentials for slot {account_num} "
                f"({email}) — aborting before commit"
            )

    def delete_previous_backup(self, account_num: str, email: str) -> None:
        """Drop a slot key's retained ``.prev`` generation (both backends).

        Best-effort, like retention itself. Called from full-key deletion,
        and on its own when a key's history stops belonging to its account —
        a renumber (swap/move) writes another account's material through the
        key, and recovery must never resurrect the displaced generation onto
        the key's new owner.
        """
        prev_file = self._prev_backup_path(account_num, email)
        try:
            if prev_file.exists():
                prev_file.unlink()
        except Exception as e:
            self._host._logger.warning(f"Failed to delete .prev file: {e}")
        if self._host.platform == Platform.MACOS:
            try:
                self._kc_delete_backup_prev(account_num, email)
            except Exception as e:
                self._host._logger.warning(
                    f"Failed to delete .prev from Keychain: {e}"
                )

    # -- previous-generation retention -------------------------------------
    #
    # One retained generation per slot, routed by the same rule as the backup
    # itself: Keychain when the Keychain is in use, ``.enc.prev`` file
    # otherwise. Retention must not *weaken* the user's storage posture — a
    # Mac whose credentials live in the Keychain must not grow a plaintext
    # copy just for recovery. Best-effort by design: the *primary* safety
    # boundary is the switch-time provenance check + unclaimed stash (whose
    # failure aborts); ``.prev`` is defense in depth for writes that were
    # classified as safe but weren't.

    def _prev_backup_path(self, account_num: str, email: str) -> Path:
        return self._host.credentials_dir / f".creds-{account_num}-{email}.enc.prev"

    def _prev_backup_username(self, account_num: str, email: str) -> str:
        return f"{self._backup_username(account_num, email)}.prev"

    def _retain_previous_backup(
        self, account_num: str, email: str, new_credentials: str
    ) -> None:
        """Retain the slot's current backup as ``.prev`` before it is replaced.

        C1: when the current generation can't be read (a locked Keychain, an
        unreadable ``.enc``), the caller's overwrite proceeds regardless — the
        write this retention protects is never conditional on retention
        succeeding. No ``.prev`` is written in that case (see the WITHDRAWN
        comment below for why a checkpoint of the incoming bytes was tried
        and reverted).
        """
        try:
            current, unreadable = self._read_account_credentials_ex(account_num, email)
        except Exception as e:  # pragma: no cover - _read swallows its own errors
            self._host._logger.warning(f"Could not read backup for retention: {e}")
            return
        if unreadable:
            # WITHDRAWN (round 10): rounds 8 and 9 tried to salvage this path by
            # checkpointing the INCOMING bytes as `.prev`. Both attempts shipped
            # a regression, each found by the next review:
            #
            #   r8  overwrote a real `.prev` holding a genuine previous
            #       generation with a duplicate of the incoming bytes.
            #   r9  guarded that with `_read_previous_backup`, which collapses
            #       absent / unreadable / corrupt to `""` -- so on a LOCKED
            #       Keychain (the very scenario this branch names) the guard
            #       reads "" and fires anyway, writing a plaintext `.enc.prev`
            #       that then WINS over the real Keychain `.prev` by the
            #       .enc-wins rule and shadows it even after the lock clears.
            #
            # Measured, r9 tree, controls first:
            #   readable + real .prev exists  -> wrote NOTHING  (guard works)
            #   readable + no prior .prev     -> wrote kc        (checkpoint)
            #   LOCKED   + real Keychain .prev-> wrote FILE      (SHADOWS IT)
            #
            # The honest position is that the true previous generation is
            # unrecoverable here, and a checkpoint that can silently outrank a
            # real one is worse than the absence it was meant to fill. Warn and
            # decline. `upstream/main` has no `.prev` at all, so this is no
            # worse there; what it is not is a cushion that lies.
            self._host._logger.warning(
                f"Could not retain previous credential generation for "
                f"account {account_num}: the current backup exists but "
                "could not be read (not absent) — no .prev recovery copy "
                "will exist for this write"
            )
            return
        if not current or current == new_credentials:
            return
        try:
            if self._use_keychain():
                self._kc_call(
                    macos_keychain.set_password,
                    SECURITY_SERVICE,
                    self._prev_backup_username(account_num, email),
                    current,
                )
            else:
                self._atomic_b64_write(
                    self._prev_backup_path(account_num, email), current
                )
        except Exception as e:
            self._host._logger.warning(
                f"Failed to retain previous credential generation for "
                f"account {account_num}: {e}"
            )

    def _read_previous_backup(self, account_num: str, email: str) -> str:
        """Read the retained previous generation. ``""`` when absent/corrupt.

        ``.enc.prev``-wins like the main backup read: a file written while the
        Keychain was unusable beats a possibly-stale Keychain copy.
        """
        prev_file = self._prev_backup_path(account_num, email)
        if prev_file.exists():
            try:
                encoded = prev_file.read_text(encoding="utf-8").strip()
                decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
                if decoded:
                    return decoded
            except Exception as e:
                self._host._logger.warning(f"Failed to read .prev file: {e}")
        if self._host.platform == Platform.MACOS:
            try:
                return self._kc_call(
                    macos_keychain.get_password,
                    SECURITY_SERVICE,
                    self._prev_backup_username(account_num, email),
                ) or ""
            except macos_keychain.KEYCHAIN_ERRORS as e:
                self._host._logger.warning(f"Failed to read .prev from Keychain: {e}")
        return ""

    # -- internal safety copies (unclaimed credentials) ----------------------
    #
    # Write-only preservation for live credential bytes a switch positively
    # attributed to someone other than the outgoing slot (invariant: never
    # overwrite the live store without preserving what was in it — the bytes
    # may be the only live copy of some account's refresh token). Entries are
    # append-only base64 files with a JSON manifest carrying the
    # classification evidence; nothing consumes them automatically — recovery
    # is the documented /login + `cswap add [--slot N]`, and these files are
    # forensic material for maintainers.
    #
    # Deliberately 0600 files on every platform, unlike the slot backups and
    # ``.prev``, which route to the macOS Keychain when it is in use: a failed
    # safety-copy write aborts the switch by design, and that abort path must
    # not inherit the Keychain's failure modes (#101/#106 — a flaky Keychain
    # would start blocking switches). On macOS this means these rare files
    # sit outside the Keychain, base64-encoded with owner-only permissions.

    def _stash_manifest_path(self) -> Path:
        return self._host.credentials_dir / ".unclaimed-manifest.json"

    def _stash_entry_path(self, entry_id: str) -> Path:
        return self._host.credentials_dir / f".unclaimed-{entry_id}.enc"

    def _read_stash_manifest_ex(self) -> tuple[dict, str]:
        """Manifest read with a three-way verdict: ok / unreadable / corrupt.

        Returns ``(entries, verdict)``. The verdict exists because "no rows"
        and "could not establish the rows" have opposite consequences here: a
        stash row is the SOLE record of a generation a prior gate pass already
        consumed, so a caller that reads a failure as "nothing stashed" POSTs
        the spent generation — the one POST the consume gate exists to
        prevent. The failure is also CORRELATED rather than independent: the
        stash exists *because* storage I/O already failed once.

        A bool cannot carry it, because the two failure modes need different
        answers and one of them is caller-dependent:

        - ``"unreadable"`` — the bytes exist and could not be read (locked
          keychain, EIO, a mode). Transient by nature and self-clearing, so
          every caller defers: it costs a pass and spends nothing.
        - ``"corrupt"`` — the bytes were read and are not a manifest. This is
          PERMANENT, so a blanket fail-closed would deadlock the slot: the
          repair is ``_write_stash_manifest`` renaming the bad file aside, and
          that runs only on a manifest WRITE, which deferring prevents. The
          adopt scan therefore decides by whether any entry bytes are actually
          at risk (see ``_stash_entry_files_exist``), and the mutator proceeds
          so the set-aside can happen at all.
        - ``"ok"`` — includes a genuinely absent manifest; no rows and nothing
          to protect read the same to every caller.

        The read, not ``exists()``, decides. ``Path.exists()`` answers False
        for an unsearchable directory on 3.13+ and RAISES on 3.12, so gating
        on it makes this verdict depend on the interpreter — the same trap
        this branch removes from the backup reader.
        """
        path = self._stash_manifest_path()
        # BYTES, so the two failure classes cannot cross. `read_text` decodes
        # inside the read, and ``UnicodeDecodeError`` is a ``ValueError``, not
        # an ``OSError`` — undecodable bytes would escape an OSError-only
        # split entirely, where the single ``except Exception`` this replaces
        # swallowed them. Decoding below puts them where they belong: the
        # bytes were readable, their content is garbage. That is corrupt.
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {}, "ok"
        except OSError as e:
            self._host._logger.warning(f"Unclaimed manifest unreadable: {e}")
            return {}, "unreadable"
        try:
            entries = json.loads(raw.decode("utf-8")).get("entries")
        except Exception as e:
            self._host._logger.warning(f"Failed to read unclaimed manifest: {e}")
            return {}, "corrupt"
        if not isinstance(entries, dict):
            # Parseable, but structurally not a manifest: ``entries`` missing
            # or the wrong type. Reading that as ok-with-no-rows re-opens the
            # gap the corrupt verdict closes — orphan entry files would bypass
            # the fail-closed condition because the verdict said nothing was
            # wrong. The rows are as unestablishable as under unparseable
            # bytes, so it gets the same verdict.
            self._host._logger.warning(
                "Unclaimed manifest parses but has no valid 'entries' member"
            )
            return {}, "corrupt"
        return entries, "ok"

    def _stash_entry_files_exist(self) -> bool:
        """Is there any stashed credential's BYTES on disk?

        Asked only when the manifest is corrupt, to tell "the mapping is gone
        and there is nothing it could have mapped" from "the mapping is gone
        and a consumed generation's only copy is sitting right here". The
        entry files are named independently of the manifest, so they answer
        even when it cannot.

        Unknowable answers TRUE: a directory we cannot list is not evidence
        that nothing is at risk, and the caller uses this to decide whether to
        spend a grant.

        ``iterdir``, NOT ``glob``. ``Path.glob`` SUPPRESSES directory-scan
        ``OSError``s, so the guard's except arm never ran: measured on 3.14.6,
        a searchable-but-unlistable credentials dir (mode 0o311) returned
        ``[]`` with a real orphan sitting right there — the manifest and the
        entry bytes stay readable through the searchable dir, so corrupt+
        orphans read as corrupt+empty and the gate POSTed the spent grant.
        ``iterdir`` raises on the same state. The same interpreter-suppression
        trap as ``Path.exists()``, third appearance in this file.

        A MISSING directory is the one ``OSError`` that answers False: no
        credentials dir means provably nothing was ever stashed.
        """
        try:
            for entry in self._host.credentials_dir.iterdir():
                name = entry.name
                if name.startswith(".unclaimed-") and name.endswith(".enc"):
                    return True
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return False

    def _read_stash_manifest(self) -> dict:
        return self._read_stash_manifest_ex()[0]

    def _write_stash_manifest(self, entries: dict) -> None:
        from claude_swap.settings import atomic_write_json

        self._host.credentials_dir.mkdir(parents=True, exist_ok=True)
        path = self._stash_manifest_path()
        # A corrupt manifest read as {} must not be silently clobbered — the
        # rows are classification evidence. Set it aside (the entry *bytes*
        # are separate files and keep being listed as orphans either way).
        # Failing closed instead would brick switching: a stash-write failure
        # aborts the switch by design.
        if path.exists():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                aside = path.with_name(
                    f"{path.name}.corrupt-{int(time.time())}"
                )
                try:
                    path.rename(aside)
                    self._host._logger.warning(
                        f"Unreadable unclaimed manifest preserved as {aside.name}"
                    )
                except OSError as e:
                    self._host._logger.warning(
                        f"Could not preserve corrupt unclaimed manifest: {e}"
                    )
        atomic_write_json(path, {"schemaVersion": 1, "entries": entries})

    def _mutate_stash_manifest(self, mutate) -> None:
        """Apply ``mutate(entries)`` to the manifest as one atomic step.

        The manifest is a single whole-file rewrite, so read-modify-write is
        only safe under mutual exclusion. Its own lock, deliberately NOT the
        slot lock: the stash writer at ``switcher.py``'s ``except LockError``
        runs precisely BECAUSE the slot lock was unavailable, so it can never
        take it, while the retire it races runs under it. Unsynchronized, the
        two rewrite the whole file from snapshots taken before the other's
        write and each drops the other's row -- measured 50 of 80 rows lost
        with three real concurrent workers, 0 lost with the same workload
        serialized.

        Losing a row is not cosmetic: ``_adopt_stashed_successor`` iterates
        manifest rows only, so a row-less entry can never be adopted, while
        ``_list_unclaimed_credentials``' glob keeps listing its bytes forever.

        This raises on failure -- ``LockError`` on a lock timeout, ``OSError``
        from ``_write_stash_manifest``'s ``atomic_write_json`` (full disk,
        read-only mount) -- and the two callers want opposite things from
        that, so neither may assume the other's handling.

        A failed STASH must be loud: callers treat a successful one as the
        license to overwrite the live store.

        A failed RETIRE must not be. It is housekeeping, and its call sites in
        ``_adopt_stashed_successor`` run after a store write that already
        advanced the slot, so a raise there would report a failed refresh for
        a slot that is in fact freshened and make the caller re-POST a spent
        generation. ``_retire_stash_entry`` therefore swallows and logs.
        """
        from claude_swap.locking import FileLock

        self._host.credentials_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(self._stash_manifest_path().with_suffix(".lock")):
            entries, verdict = self._read_stash_manifest_ex()
            if verdict == "unreadable":
                # Reading an unreadable-but-VALID manifest as `{}` does not
                # merely miss rows. ``_write_stash_manifest`` below finds the
                # same file unparseable, renames the healthy manifest aside,
                # and writes a fresh one holding only this mutation's row —
                # orphaning every previously mapped successor in one step,
                # in exactly the correlated setting where a stash is being
                # written because storage already misbehaved.
                #
                # Raising suits both callers: a failed STASH must be loud (a
                # successful one is the licence to overwrite the live store),
                # and a failed RETIRE is already swallowed by
                # ``_retire_stash_entry``.
                #
                # CORRUPT deliberately falls through: the set-aside below is
                # the only repair, and it can only happen on a write.
                raise CredentialReadError(
                    "the unclaimed manifest is unreadable; refusing to rewrite "
                    "it from an empty read, which would orphan every stashed "
                    "successor it maps"
                )
            mutate(entries)
            self._write_stash_manifest(entries)

    def _write_unclaimed_credential(self, credentials: str, context: dict) -> str:
        """Stash a credential of unknown provenance. Returns the entry id.

        Raises on any failure — callers use a successful stash as the license
        to overwrite the live store, so a failed one must be loud. The entry
        file is written before the manifest: an entry without manifest metadata
        is recoverable; a manifest row without bytes is not.
        """
        import hashlib
        import secrets
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        digest = hashlib.sha256(credentials.encode("utf-8")).hexdigest()[:12]
        # Nonce keeps ids unique even for identical bytes preserved in the
        # same second — append-only means no write may ever land on an
        # existing id.
        entry_id = f"{ts}-{digest}-{secrets.token_hex(3)}"
        self._atomic_b64_write(self._stash_entry_path(entry_id), credentials)
        row = {
            "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **context,
        }
        self._mutate_stash_manifest(lambda entries: entries.update({entry_id: row}))
        return entry_id

    def _list_unclaimed_credentials(self) -> dict[str, dict]:
        """Manifest entries by id, including orphaned entry files (no metadata)."""
        entries = dict(self._read_stash_manifest())
        try:
            for path in self._host.credentials_dir.glob(".unclaimed-*.enc"):
                entry_id = path.name[len(".unclaimed-"):-len(".enc")]
                entries.setdefault(entry_id, {"createdAt": None})
        except OSError:
            pass
        return entries

    def _read_unclaimed_credential(self, entry_id: str) -> tuple[str, bool]:
        """Decode one stashed credential's bytes.

        Returns ``(value, unreadable)`` -- the same shape as
        ``_read_account_credentials_ex``. ``unreadable`` is True when the
        entry file EXISTS but its bytes could not be read (locked
        Keychain-adjacent volume, mid-unmount, transient EIO/permissions).
        A stash entry exists only because a prior gate pass already
        consumed a grant and could not persist the successor -- the entry
        is the SOLE copy of that generation, so the caller must not treat
        a transient read failure as "nothing to adopt".

        A genuinely ABSENT entry, or one that exists but is CORRUPT
        (undecodable base64 -- its bytes are unrecoverable, not merely
        inaccessible right now), both return ``("", False)``: there is
        nothing a retry could ever adopt either way.
        """
        path = self._stash_entry_path(entry_id)
        try:
            encoded = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return "", False
        except OSError as e:
            self._host._logger.warning(
                f"Unclaimed credential {entry_id} unreadable: {e}"
            )
            return "", True
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8"), False
        except Exception as e:
            self._host._logger.warning(
                f"Failed to decode unclaimed credential {entry_id}: {e}"
            )
            return "", False

    def _remove_unclaimed_credential(self, entry_id: str) -> None:
        """Delete a stash entry (bytes + manifest row) after it was adopted."""
        try:
            self._stash_entry_path(entry_id).unlink(missing_ok=True)
        except OSError as e:
            self._host._logger.warning(
                f"Failed to remove unclaimed credential {entry_id}: {e}"
            )
        self._mutate_stash_manifest(lambda entries: entries.pop(entry_id, None))
