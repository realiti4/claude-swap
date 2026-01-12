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


class LockError(ClaudeSwitchError):
    """Error acquiring lock."""

    pass


class AccountNotFoundError(ClaudeSwitchError):
    """Account not found."""

    pass


class ValidationError(ClaudeSwitchError):
    """Validation error."""

    pass
