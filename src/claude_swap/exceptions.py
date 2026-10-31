"""Custom exceptions for Claude Switch."""


class ClaudeSwitchError(Exception):
    """Base exception for Claude Switch errors."""

    pass


class CredentialError(ClaudeSwitchError):
    """Error related to credential operations."""

    pass


class CredentialReadError(CredentialError):
    """Failed to read credentials."""

    pass


class CredentialWriteError(CredentialError):
    """Failed to write credentials."""

    pass


class ConfigError(ClaudeSwitchError):
    """Error related to configuration operations."""

    pass


class SwitchError(ClaudeSwitchError):
    """Error during account switch operation."""

    pass


class TargetCredentialDead(SwitchError):
    """A switch target's stored credential was just proven dead (a profile
    401 followed by a refresh that answered a permanent auth error), raised
    by ``_perform_switch``'s pre-lock liveness probe after striking the slot.
    Callers with more than one candidate catch this and advance; a caller
    with only one target converts it to a ``target-credential-dead`` noop.
    """

    pass


class TargetCredentialUnconfirmed(TargetCredentialDead):
    """A switch target's profile probe got a real 401, but the escalation
    through ``consume_backup_grant`` (or the re-probe of a freshened
    credential) came back with no verdict — lock contention, a transient
    refresh failure, or a transport failure on the re-probe. Not proven
    dead, so ``_perform_switch`` does not strike the slot; but a proven
    refusal all the same, so it must not be activated blind. Subclasses
    ``TargetCredentialDead`` so every multi-candidate caller's existing
    ``except TargetCredentialDead`` advances past it unchanged; ``switch_to``
    tells the two apart to report a distinct reason/message (there is no
    next candidate to advance to).
    """

    pass


class SessionError(ClaudeSwitchError):
    """Error setting up or launching a session-mode profile."""

    pass


class LockError(ClaudeSwitchError):
    """Error acquiring lock."""

    pass


class ClaudeCodeLockTimeout(LockError):
    """Timed out acquiring one of Claude Code's own advisory locks.

    Raised when ``~/.claude.lock`` / ``~/.claude.json.lock`` stays held past
    our bounded wait — usually Claude Code mid-token-refresh. Nothing has been
    mutated when this raises; the operation is safe to retry.
    """

    pass


class AccountNotFoundError(ClaudeSwitchError):
    """Account not found."""

    pass


class ValidationError(ClaudeSwitchError):
    """Validation error."""

    pass


class TransferError(ClaudeSwitchError):
    """Error during account export or import."""

    pass


class MigrationError(ClaudeSwitchError):
    """Error migrating the backup directory between layouts (e.g. legacy → XDG)."""

    pass


class MigrationIncomplete(ClaudeSwitchError):
    """A one-time data migration could not finish for every record.

    Raised by run-once migrations (see ``migrations.py``) when some entries
    failed or the source backend was inaccessible. The migration runner treats
    this as "not applied" so the migration is retried on the next run rather
    than being recorded as done with records left behind.
    """

    pass
