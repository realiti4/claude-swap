"""``cswap run --auto``: place a new Claude session on an account and launch it.

Placement uses the balance policy (``balance.rank_accounts``) with the
fleet's server-reported usage and the local busy count (live managed
sessions, reservations included). The default login's account (lane 0) is
held out — its Claude owns that lineage and is already spending that 5h
window — unless nothing else can take the session; then the session gets a
read-only copy of lane 0's current access token (never its refresh token).
``candidates`` is the caller's job to narrow: an account with a live
``cswap run N`` session (that Claude owns its lineage too) is excluded
before it ever reaches :func:`choose_placement`, which only ranks whatever
pool it is handed.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from claude_swap.autoswitch import FRESHEN_BUFFER_MS, quarantined_numbers
from claude_swap.balance import (
    AccountScore,
    BalanceParams,
    params_from_settings,
    rank_accounts,
)
from claude_swap.exceptions import SessionError
from claude_swap.managed_refresh import (
    account_config,
    busy_by_slot,
    resolve_access_credential,
)
from claude_swap.managed_sessions import (
    LAUNCH_LOCK_TIMEOUT_S,
    SOURCE_BACKUP,
    SOURCE_LANE0,
    AccountRef,
    ManagedEntry,
    ManagedSessionRegistry,
    create_managed_profile,
    new_session_id,
    process_stamp,
    remove_managed_profile,
    write_hooks_file,
)
from claude_swap.models import Platform
from claude_swap.printer import warning
from claude_swap.session import (
    SessionManager,
    announce_launch,
    resolve_claude_binary,
    session_profile_env,
    warn_auth_override_env,
    warn_config_dir_override,
)
from claude_swap.session_credentials import write_session_credential
from claude_swap.settings import load_settings, parse_model_names

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

_logger = logging.getLogger("claude-swap")


@dataclass(frozen=True)
class Placement:
    """Where a new managed session should run, and why.

    The "why" is ``score``: the registry row keeps only the constant
    ``REASON_LAUNCH``, so the ranking that actually picked this account
    survives nowhere else. ``run_auto`` logs it at debug (``cswap --debug``,
    and the log file keeps debug records either way), which is what a user
    asking "why this account?" after the fact has to go on.
    """

    number: str
    account: AccountRef
    source: str          # SOURCE_BACKUP or SOURCE_LANE0
    score: AccountScore


def choose_placement(
    *,
    candidates: Sequence[str],
    identities: Mapping[str, AccountRef],
    usage: Mapping[str, dict | None],
    lane0: str | None,
    busy: Mapping[str, int],
    now: float,
    models: Sequence[str],
    params: BalanceParams,
) -> Placement | None:
    """Rank ``candidates`` under the balance policy and place on the best one.

    Lane 0 is dropped from the ranked pool — its Claude owns that lineage
    and is already spending its 5h window — and only retried, as a last
    resort, when nothing else in ``candidates`` is eligible: it is then
    scored on its own and returned with ``source="lane0"``, but only if it
    is itself one of ``candidates``. Returns None when nothing (including
    lane 0) can take the session.
    """
    pool = [n for n in candidates if n != lane0 and n in identities]
    ranked = rank_accounts(
        {n: usage.get(n) for n in pool},
        now=now,
        models=models,
        busy_sessions={n: busy.get(n, 0) for n in pool},
        params=params,
    )
    if ranked:
        best = ranked[0]
        return Placement(best.account, identities[best.account], SOURCE_BACKUP, best)
    if lane0 is None or lane0 not in candidates or lane0 not in identities:
        return None
    lane = rank_accounts(
        {lane0: usage.get(lane0)},
        now=now,
        models=models,
        busy_sessions={lane0: busy.get(lane0, 0)},
        params=params,
    )
    if not lane:
        return None
    return Placement(lane0, identities[lane0], SOURCE_LANE0, lane[0])


WINDOWS_REFUSAL = (
    "cswap run --auto is not supported on Windows yet: managed sessions "
    "share history through symlinks and rely on exec keeping the process "
    "id. Use `cswap run NUM` instead."
)
NO_ACCOUNT_MESSAGE = (
    "No account has room for a managed session right now (usage unknown or "
    "every account at its limit). Check `cswap list`, or start one "
    "explicitly with `cswap run NUM`."
)


def _decision_usage(
    switcher: ClaudeAccountSwitcher, fetch: set[str] | None
) -> dict[str, dict | None]:
    """Decision-grade usage per slot; sentinels and unknowns become None."""
    usage: dict[str, dict | None] = {}
    for number, entry in switcher.usage_entries_by_account(fetch=fetch).items():
        value = entry.decision_value()
        usage[number] = value if isinstance(value, dict) else None
    return usage


def _theme_for(switcher: ClaudeAccountSwitcher, number: str, email: str) -> str:
    """The slot's stored Claude theme, so a managed session does not look
    different from the account's own ``cswap run N`` profile."""
    theme = account_config(switcher, number, email).get("theme")
    return theme if isinstance(theme, str) and theme else "dark"


def _candidate_identities(
    switcher: ClaudeAccountSwitcher,
) -> dict[str, AccountRef]:
    """The accounts a new managed session may be placed on, by slot number.

    Narrowing the pool is the caller's job, not ``choose_placement``'s
    (which only ranks what it is handed): out go quarantined slots, API-key
    accounts (a managed profile is OAuth-shaped), slots with no stored
    email, and any account that already has a live ``cswap run N`` session
    here — that Claude owns the account's lineage and is already spending
    its 5h window, exactly as lane 0 does.
    """
    quarantined = quarantined_numbers(switcher.backup_dir)
    identities: dict[str, AccountRef] = {}
    for number in switcher.switchable_account_numbers():
        if number in quarantined or switcher.account_kind_for(number) == "api_key":
            continue
        ident = switcher.account_identity(number)
        if not ident["email"] or switcher.live_session_pids_for(number, ident["email"]):
            continue
        identities[number] = AccountRef(ident["email"], ident["organizationUuid"])
    return identities


class _ProfileCollision(SessionError):
    """Something was already at the new session's profile path.

    Managed ids are freshly minted per launch, so a collision means the
    directory is somebody else's — a live session whose reservation this
    launch could not see, or an orphan still inside the sweeper's grace
    window. This launch put nothing on disk there, so the rollback must
    leave that directory (and its keychain item) alone; the orphan sweep
    reclaims it on its own terms. It needs no branch of its own to get
    that: ``_LaunchState.profile_dir`` is still None here, which is what
    keeps the rollback off the directory.
    """


@dataclass
class _LaunchState:
    """What a failed launch has to undo, beyond its reservation.

    ``profile_dir`` is set only once ``create_managed_profile`` has
    RETURNED, so a rollback can never delete a directory this launch did
    not create. Everything before that point — resolving the credential
    (the common failure: a non-ok resolution, or a Ctrl-C during the
    network refresh), reading the account's theme — happens while the path
    still belongs to whoever is already there, if anyone is, and 8 hex
    digits of id are not the guarantee ``create_managed_profile``
    deliberately refuses to rely on.
    """

    profile_dir: Path | None = None


def _rollback(
    registry: ManagedSessionRegistry, session_id: str, session_dir: Path | None
) -> None:
    """Undo a launch that failed after its reservation was written.

    Each step is best-effort and independent: this runs from an ``except``
    block, so an exception escaping here would replace the failure the
    user actually needs to see (a contended registry lock is the likely
    one) and skip every step behind it. Whatever could not be undone is
    logged and left to the dead-PID sweep — the reservation carries this
    process's pid, so it is dead the moment this process exits.

    ``session_dir`` is None when this launch did not create the profile.
    """
    try:
        registry.remove(session_id)
    except Exception as e:  # noqa: BLE001 - the original failure must survive
        _logger.warning(
            f"Could not withdraw the reservation for managed session "
            f"{session_id} ({e}); the sweep will reclaim it."
        )
    if session_dir is None:
        return
    try:
        remove_managed_profile(session_dir)
    except Exception as e:  # noqa: BLE001 - same
        _logger.warning(f"Could not remove the managed profile {session_dir}: {e}")


def _prepare_profile(
    switcher: ClaudeAccountSwitcher,
    manager: SessionManager,
    registry: ManagedSessionRegistry,
    entry: ManagedEntry,
    placement: Placement,
    state: _LaunchState,
    *,
    now_ms: float,
) -> Path:
    """Seed the reserved session's profile; returns its --settings path.

    Records the profile directory on ``state`` the moment it is ours, and
    not before — see ``_LaunchState``.

    Raises:
        _ProfileCollision: The profile directory already existed.
        SessionError: No usable token, or the credential could not be
            written.
    """
    session_dir = registry.session_dir(entry.session_id)
    resolution = resolve_access_credential(
        switcher, entry.account, now_ms=now_ms, buffer_ms=FRESHEN_BUFFER_MS
    )
    if (
        resolution.status != "ok"
        or resolution.credential is None
        or resolution.oauth_account is None
    ):
        raise SessionError(
            f"Could not get a usable token for Account-{placement.number} "
            f"({entry.account.email}): {resolution.status}. Retry, or check "
            "it with `cswap list`."
        )
    try:
        create_managed_profile(
            session_dir,
            theme=_theme_for(switcher, placement.number, entry.account.email),
        )
    except FileExistsError as e:
        raise _ProfileCollision(
            f"A profile already exists at {session_dir}; refusing to reuse "
            "it. Try again — a new session id is minted per launch."
        ) from e
    state.profile_dir = session_dir
    result = write_session_credential(
        session_dir, entry.account, resolution.credential,
        resolution.oauth_account, registry=registry,
        # The profile was created moments ago, so nothing is in the keychain
        # to shadow what is written here: claude finds no item and reads the
        # plaintext, which holds the right token. Refusing over a keychain
        # that cannot be read or written would block a launch that works —
        # an ssh session, a Mac whose login keychain is not unlocked.
        require_keychain=False,
    )
    # `ok` is the whole test; a plaintext-only success has a reason that is
    # not "ok", and `keychain_written` is what says the token is unprotected.
    if not result.ok:
        raise SessionError(
            f"Could not write the managed session's credential ({result.reason})."
        )
    if switcher.platform == Platform.MACOS and not result.keychain_written:
        warning(
            "This session's access token is stored in a file inside its "
            "profile, not in the Keychain: the Keychain item could not be "
            "read or written (a locked login keychain, or no Keychain "
            "access from this shell). The session itself works normally."
        )
    # History is always shared: --resume must work whichever account a
    # session is placed on, and keeps working if it is ever moved.
    manager._sync_sharing(session_dir, share=True, share_history=True)
    hooks_path = write_hooks_file(session_dir)
    registry.update(
        entry.session_id,
        access_fingerprint=result.fingerprint,
        source=resolution.source,
    )
    return hooks_path


def run_auto(
    switcher: ClaudeAccountSwitcher,
    claude_args: list[str],
    *,
    manager: SessionManager | None = None,
    clock: Callable[[], float] = time.time,
) -> NoReturn:
    """Place, prepare and exec a managed session. Never returns on success.

    Raises:
        SessionError: Windows, no claude on PATH, no account with room, or
            the profile could not be seeded. Anything that fails after the
            account is reserved takes the reservation and the half-built
            profile back down with it.
    """
    if switcher.platform == Platform.WINDOWS:
        raise SessionError(WINDOWS_REFUSAL)
    claude_bin = resolve_claude_binary()
    manager = manager or SessionManager(switcher)

    preset = os.environ.get("CLAUDE_CONFIG_DIR")
    if preset:
        warn_config_dir_override(preset)
    warn_auth_override_env()

    # The launch path waits longer for the registry lock than the pollers
    # do: `allocate` holds it across a `ps` per entry and a record read per
    # entry, so a concurrent `cswap run --auto` must not fail on a lock
    # error just because the other one is probing a few slow processes.
    registry = ManagedSessionRegistry(
        switcher.backup_dir, clock=clock, lock_timeout=LAUNCH_LOCK_TIMEOUT_S
    )
    live = registry.sweep().live

    settings = load_settings(switcher.backup_dir)
    models = parse_model_names(settings.model)
    params = params_from_settings(settings)
    identities = _candidate_identities(switcher)
    candidates = list(identities)
    lane0 = switcher.current_account_number()
    now = clock()

    def place(usage: Mapping[str, dict | None], busy_by_account: Mapping[AccountRef, int]):
        return choose_placement(
            candidates=candidates,
            identities=identities,
            usage=usage,
            lane0=lane0,
            # The engine's lane-0 ranking re-keys the same counts the same
            # way; a slot with no busy session is absent and reads as 0.
            busy=busy_by_slot(switcher, busy_by_account),
            now=now,
            models=models,
            params=params,
        )

    # Cached usage first (instant). Only when that places nothing, or only
    # on lane 0, is a fetch worth the launch latency before settling.
    usage = _decision_usage(switcher, fetch=set())
    probe = place(usage, registry.busy_counts(live))
    if probe is None or probe.source == SOURCE_LANE0:
        usage = _decision_usage(switcher, fetch=None)

    placement: Placement | None = None

    def choose(busy_by_account: Mapping[AccountRef, int]):
        nonlocal placement
        placement = place(usage, busy_by_account)
        return None if placement is None else (placement.account, placement.source)

    session_id = new_session_id()
    pid = os.getpid()  # exec keeps it: this reservation becomes the session
    entry = registry.allocate(session_id, choose, pid=pid, proc_start=process_stamp(pid))
    if entry is None or placement is None:
        raise SessionError(NO_ACCOUNT_MESSAGE)

    # Why this account, for the user who asks afterwards: the recorded
    # reason on the row is the constant "launch", so the ranking that
    # actually decided it only survives here.
    _logger.debug(
        f"Managed session {session_id} placed on Account-{placement.number} "
        f"({entry.account.email}, source {placement.source}): {placement.score}"
    )

    session_dir = registry.session_dir(session_id)
    state = _LaunchState()
    try:
        hooks_path = _prepare_profile(
            switcher, manager, registry, entry, placement, state, now_ms=now * 1000
        )
    except BaseException:
        # state.profile_dir is None for everything that failed before the
        # profile existed, a collision with somebody else's directory
        # included: only a directory this launch created is removed.
        _rollback(registry, session_id, state.profile_dir)
        raise

    # Handing the terminal over is the last thing that can fail: `_exec`
    # never returns here (run_auto refuses Windows, so this is execvpe),
    # and the way out of it is an OSError saying the session never started
    # — a broken pipe under the banner, or execvpe itself failing.
    # UnicodeError joins it because the banner prints an account's email,
    # which a non-ASCII address under a byte-oriented locale cannot
    # encode. Anything wider would catch a test double's fake `_exec` and
    # roll a launch back that in production had already handed the process
    # over. The narrowness costs nothing: an exception that escapes here
    # (printing to a CLOSED stdout raises ValueError, not OSError) takes
    # this process down, and the reservation carries this process's pid,
    # so the next sweep reclaims the row and the profile anyway.
    try:
        announce_launch(
            placement.number, entry.account.email, f"[managed session {session_id}]"
        )
        if placement.source == SOURCE_LANE0:
            warning(
                "Every other account is at its limit or unknown: this session "
                "borrows the default login's current token (read-only)."
            )
        manager._exec(
            claude_bin, ["--settings", str(hooks_path), *claude_args],
            env=session_profile_env(session_dir),
        )
    except (OSError, UnicodeError):
        _rollback(registry, session_id, state.profile_dir)
        raise
