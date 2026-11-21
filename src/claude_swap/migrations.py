"""One-time, run-once data migrations for claude-swap.

A small, boring home for compatibility migrations so they don't pollute the
core switch/read/write flow in :mod:`claude_swap.switcher`. Each migration:

- is **idempotent** and **self-guarded** (safe to run twice, safe even if the
  state file is missing or corrupt),
- returns ``True`` when it *completed* (runner records it as applied),
- returns ``False`` when it was *skipped / not applicable* (runner records
  nothing, so a later-restored backup can still trigger it),
- raises :class:`~claude_swap.exceptions.MigrationIncomplete` (or any other
  exception) when it *partially failed* — the runner logs it and leaves it
  unmarked so the next run retries.

Applied migrations are tracked in ``<backup_dir>/.migrations.json``:

    {"version": 1, "applied": {"macos_keyring_to_security": "<iso-timestamp>"}}

Run once at switcher construction (see ``ClaudeAccountSwitcher.__init__``);
after the state file records a migration it short-circuits with a single tiny
file read and never touches the source backend again.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from claude_swap import macos_keychain
from claude_swap.exceptions import MigrationIncomplete
from claude_swap.fsutil import atomic_write_text
from claude_swap.models import Platform, get_timestamp
from claude_swap.switcher import KEYRING_SERVICE

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

STATE_FILENAME = ".migrations.json"
STATE_VERSION = 1

# How long a legacy read waits for a person to answer the Keychain permission
# window: time to type the login password and click. See
# ``migrate_macos_keyring_to_security``.
_PROMPT_WAIT = 60.0


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------


def _state_path(switcher: "ClaudeAccountSwitcher") -> Path:
    return switcher.backup_dir / STATE_FILENAME


def _load_applied(switcher: "ClaudeAccountSwitcher") -> dict:
    """Return the ``{migration_id: timestamp}`` map; {} if missing or corrupt.

    A missing or unparseable state file is treated as "nothing applied" so a
    corrupt file can never permanently block a migration.
    """
    path = _state_path(switcher)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    applied = data.get("applied") if isinstance(data, dict) else None
    return applied if isinstance(applied, dict) else {}


def _mark_applied(switcher: "ClaudeAccountSwitcher", migration_id: str) -> None:
    """Record ``migration_id`` as applied, written atomically.

    Preserves any previously-recorded migrations. Published through the shared
    ``fsutil.atomic_write_text`` (0600 temp beside the target, rename), like
    every other local state file; this one holds no secrets, the mode simply
    matches.
    """
    path = _state_path(switcher)
    applied = _load_applied(switcher)
    applied[migration_id] = get_timestamp()
    content = json.dumps({"version": STATE_VERSION, "applied": applied}, indent=2)
    atomic_write_text(path, content)


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def migrate_macos_keyring_to_security(switcher: "ClaudeAccountSwitcher") -> bool:
    """Move macOS backup credentials from the legacy ``keyring`` service to the
    ``security`` service.

    macOS stores per-account backup credentials in the Keychain via the
    ``security`` CLI under ``SECURITY_SERVICE`` (see
    :mod:`claude_swap.macos_keychain`). Versions before 0.19 stored them through
    the third-party ``keyring`` library under ``KEYRING_SERVICE`` instead. Source
    and destination are *different* services, so old and new items coexist during
    a safe write → verify → delete (no risk window).

    The legacy items are read and deleted through ``security`` too (the
    ``keyring`` library is no longer a dependency). Reading the secret of an item
    another app created goes through the item's ACL and may prompt once; deleting
    an item does not consult the ACL, so the cleanup never prompts. A failed
    delete leaves a harmless orphan behind (logged; ``purge`` sweeps it up).

    The prompt asks for the login password, and under the wrapper's default
    bound the read's ``security`` process is usually killed before a person has
    answered: the window closes with it on macOS 15.7.7 (issue #589) and stays
    open with no reader left on 27.2.1 (measured). When a person is running the
    command (``switcher.interactive``) and an attribute-only lookup finds the
    legacy item, the read is announced on stderr and waits ``_PROMPT_WAIT``
    instead. The lookup only chooses the bound: an item it does
    not find is still read, under the default bound, and that read decides
    whether the item is absent. A prompt left unanswered ends the attempt, so an
    unattended terminal waits once per run, not once per account; accounts
    already moved stay moved and the rest retry on the next run. Every other
    caller keeps the default bound on every read.

    Returns ``True`` (completed) on macOS once every account is accounted for
    (including the benign "already migrated / nothing to do" cases); ``False``
    (skip) on non-macOS or when there's no readable sequence yet. Raises
    :class:`MigrationIncomplete` if any account could not be safely relocated, so
    the runner retries on the next launch rather than marking it done.
    """
    if switcher.platform != Platform.MACOS:
        return False
    if not switcher.sequence_file.exists():
        return False  # No managed accounts yet — let a later restore migrate.

    data = switcher._get_sequence_data()
    if data is None:
        # sequence.json exists but is corrupt. Never mark applied: a user who
        # repairs/restores it must still get the migration.
        return False

    accounts = data.get("accounts", {})
    if not accounts:
        return True  # Readable sequence, nothing to migrate → done.

    # Pre-check: anything already in the new security service is done. New installs
    # and already-migrated users have every account here, so they never touch the
    # legacy service at all. Only the still-missing accounts proceed below; on a
    # retry this also narrows work to the accounts that actually failed last time.
    #
    # Read the security service *directly* (not the transparent .enc-wins backup
    # methods): this migration's job is the Keychain specifically, so a fallback
    # .enc must not be mistaken for "already migrated". A down Keychain here is not
    # "nothing to migrate" — defer and retry rather than skip real entries.
    try:
        pending = {
            account_num: info
            for account_num, info in accounts.items()
            if not switcher._kc_read_backup(account_num, info.get("email", ""))
        }
    except macos_keychain.KEYCHAIN_ERRORS as e:
        # Keychain unusable (locked/denied/missing) — defer, don't skip real
        # entries. A programming error is NOT caught here; it propagates.
        raise MigrationIncomplete(
            f"Keychain unavailable, deferring macOS keyring migration: {e}"
        ) from e
    if not pending:
        return True  # All accounts already in the security service.

    # account-None-{email} maps to a slot only when its email is unique.
    email_counts = Counter(info.get("email", "") for info in accounts.values())

    announced = False
    unanswered = False  # a prompt went unanswered: stop reading for this run
    timed_out = False

    def _read_old(username: str) -> str | None:
        """Read a legacy ``KEYRING_SERVICE`` item: its value, ``""`` if absent,
        ``None`` if the read failed (logged). A failed read (e.g. a locked
        Keychain) is a real failure, never "nothing to migrate"."""
        nonlocal announced, unanswered, timed_out
        prompted = switcher.interactive and macos_keychain.item_exists(
            KEYRING_SERVICE, username
        )
        try:
            if prompted:
                if not announced:
                    announced = True
                    print(
                        "claude-swap: moving saved logins from an older version. "
                        "macOS may ask for your login password for each one: "
                        'enter it and choose "Always Allow" (waiting up to '
                        f"{_PROMPT_WAIT:.0f} s)",
                        file=sys.stderr,
                    )
                value = macos_keychain.get_password(
                    KEYRING_SERVICE, username, timeout=_PROMPT_WAIT
                )
            else:
                value = macos_keychain.get_password(KEYRING_SERVICE, username)
        except Exception as e:  # noqa: BLE001
            switcher._logger.warning(
                f"macos_keyring_to_security: read of {username} failed: {e}"
            )
            if isinstance(e, macos_keychain.KeychainTimeout):
                timed_out = True
                if prompted:
                    unanswered = True
            return None
        return value or ""

    def _delete_old(username: str) -> None:
        """Best-effort removal of a legacy ``KEYRING_SERVICE`` item. Never raises:
        the copy is already verified, so a leftover is harmless cruft that
        ``purge`` sweeps up. An absent item (rc 44) counts as removed."""
        try:
            macos_keychain.delete_password(KEYRING_SERVICE, username)
        except Exception as e:  # noqa: BLE001 - best effort
            switcher._logger.warning(
                f"macos_keyring_to_security: legacy keyring entry {username} was "
                f"left behind (delete failed: {e}); harmless — remove manually or "
                "via purge"
            )

    migrated = 0
    failed = 0

    for account_num, info in pending.items():
        if unanswered:
            failed += 1  # not attempted; the next run retries it
            continue

        email = info.get("email", "")
        canonical = f"account-{account_num}-{email}"
        none_user = f"account-None-{email}"

        # --- pick a source (canonical wins) -------------------------------
        creds = _read_old(canonical)
        if creds is None:
            failed += 1
            continue

        source_username = canonical
        if not creds and str(account_num) != "None" and email_counts[email] == 1:
            creds = _read_old(none_user)
            if creds is None:
                failed += 1
                continue
            if creds:
                source_username = none_user

        if not creds:
            # Nothing in the legacy service for this slot (e.g. added on the new
            # version, or an ambiguous account-None we won't touch). Benign — not
            # a failure.
            continue

        # --- write + verify before deleting the source ------------------------
        # Keychain-only helpers: this migration targets the security service, so
        # it must not be diverted to .enc files by the transparent backup methods.
        try:
            switcher._kc_write_backup(account_num, email, creds)
            readback = switcher._kc_read_backup(account_num, email)
        except Exception as e:  # noqa: BLE001
            switcher._logger.warning(
                f"macos_keyring_to_security: write/read-back for {canonical} failed: {e}"
            )
            # A partial/garbage security item must not shadow the still-intact
            # legacy entry. Drop it; the retry rewrites it.
            switcher._delete_backup_keychain_quiet(account_num, email)
            failed += 1
            continue

        if readback != creds:
            switcher._logger.warning(
                f"macos_keyring_to_security: read-back mismatch for {canonical}; "
                "discarding the security item and leaving the legacy entry in place"
            )
            switcher._delete_backup_keychain_quiet(account_num, email)
            failed += 1
            continue

        # Data is safely in the security service now → remove the legacy source
        # and, when the email is unique, its stale account-None duplicate. An
        # ambiguous account-None (shared email) is left alone; purge sweeps it.
        _delete_old(source_username)
        if (
            source_username != none_user
            and str(account_num) != "None"
            and email_counts[email] == 1
        ):
            _delete_old(none_user)
        migrated += 1

    if migrated:
        print(
            f"claude-swap: migrated {migrated} macOS credential(s) from the legacy "
            "keyring service into the Keychain via security",
            file=sys.stderr,
        )

    if failed:
        hint = (
            ". If macOS showed a Keychain permission window, run `cswap list` in a "
            'terminal and approve it (login password, then "Always Allow")'
            if timed_out
            else ""
        )
        raise MigrationIncomplete(
            f"{failed} account(s) could not be migrated to the security service; "
            f"will retry on next run{hint}"
        )
    return True


# Registry of (id, fn). Order matters if migrations ever depend on each other.
# Retired ids stay valid in an old state file (``windows_keyring_to_files``, the
# Credential Manager → files move that needed the keyring library) and are simply
# carried along.
MIGRATIONS: list[tuple[str, Callable[["ClaudeAccountSwitcher"], bool]]] = [
    ("macos_keyring_to_security", migrate_macos_keyring_to_security),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_migrations(switcher: "ClaudeAccountSwitcher") -> None:
    """Run any not-yet-applied migrations. Never raises.

    A no-op on fresh installs (backup dir not yet materialized — preserves the
    lazy-dir invariant) and once the state file records every migration. A
    failing migration is logged and left unmarked so it retries next run; it
    must never abort switcher construction.
    """
    if not switcher.backup_dir.exists():
        return

    applied = _load_applied(switcher)
    for migration_id, fn in MIGRATIONS:
        if migration_id in applied:
            continue
        try:
            completed = fn(switcher)
        except Exception as e:  # noqa: BLE001 - migrations must never brick the tool
            switcher._logger.warning(
                f"Migration {migration_id} did not complete (will retry): {e}"
            )
            continue
        if completed:
            try:
                _mark_applied(switcher, migration_id)
            except Exception as e:  # noqa: BLE001
                switcher._logger.warning(
                    f"Migration {migration_id} ran but recording it failed "
                    f"(will re-run next time): {e}"
                )
