"""The provider seam.

Deliberately narrow. ``ClaudeAccountSwitcher`` is 7000 lines, most of it
credential-provenance machinery (lineage verdicts, the profile oracle, the
unclaimed stash) with no Codex analogue — extracting a base class from it would
be a rewrite wearing a refactor's clothes. So the seam is placed where the two
providers genuinely agree: the read model, and the verbs a UI needs to drive an
account list.

Every method below already exists on ``ClaudeAccountSwitcher`` under exactly
this name, so it satisfies the Protocol structurally with no adapter. New
providers implement this and nothing more.

``runtime_checkable`` gives ``issubclass`` on method *presence* only — it does
not verify signatures. That is enough for its job here: catching a renamed verb
at test time rather than at runtime on one provider.

The Protocol is deliberately **method-only**, even though every implementation
must also carry a ``provider_id: str`` class attribute (matching
``AccountSnapshot.provider``: "claude" | "codex"). A ``runtime_checkable``
Protocol that declares a data member supports ``isinstance`` but raises
``TypeError`` on ``issubclass`` — and ``issubclass`` is the check worth having,
because constructing a ``ClaudeAccountSwitcher`` runs migrations and touches the
account store, which a structural conformance test has no business doing.
``provider_id`` is therefore contract-by-docstring, asserted directly in
``tests/test_codex_seam.py`` for each provider.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from claude_swap.models import AccountsSnapshot


@runtime_checkable
class ProviderSwitcher(Protocol):
    """What a provider must offer for cswap's UIs to drive it.

    Implementations must additionally define ``provider_id: str`` — see the
    module docstring for why it cannot live in the Protocol body.
    """

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        """One coherent pass over every managed account."""
        ...

    def current_account_number(self) -> str | None:
        """The active slot, or None when no managed account is active."""
        ...

    def switch_to(self, *args, **kwargs):
        """Activate a specific account."""
        ...

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        """Forget an account."""
        ...

    def set_alias(self, identifier: str, alias: str) -> tuple[str, str]:
        """Give an account a short name."""
        ...

    def unset_alias(self, identifier: str) -> str:
        """Drop an account's alias."""
        ...

    def switchable_account_numbers(self) -> list[str]:
        """Slots eligible for automatic rotation."""
        ...

    def set_account_disabled(self, identifier: str, disabled: bool) -> None:
        """Hold an account out of (or return it to) automatic rotation."""
        ...

    def resolve_account(self, identifier: str) -> tuple[str, str, str]:
        """Resolve a number/email/alias to a concrete account."""
        ...
