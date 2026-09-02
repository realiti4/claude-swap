"""The provider seam: read-model fields and the Protocol both providers satisfy."""

from __future__ import annotations

from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageEntry


def _snapshot(**kw) -> AccountSnapshot:
    base = dict(
        number="1",
        email="a@example.com",
        org_name="",
        org_uuid="",
        is_active=True,
        kind="oauth",
        switchable=True,
        usage=UsageEntry(),
    )
    base.update(kw)
    return AccountSnapshot(**base)


def test_account_snapshot_defaults_to_claude_provider():
    """Existing construction sites pass no provider and must keep working."""
    assert _snapshot().provider == "claude"


def test_account_snapshot_accepts_an_explicit_provider():
    assert _snapshot(provider="codex").provider == "codex"


def test_accounts_snapshot_defaults_to_claude_provider():
    snap = AccountsSnapshot(active_number="1", accounts=(), taken_at=0.0)
    assert snap.provider == "claude"


def test_claude_switcher_declares_its_provider_id():
    assert ClaudeAccountSwitcher.provider_id == "claude"


def test_claude_switcher_satisfies_the_provider_protocol():
    """The seam is defined so the existing Claude switcher already fits it.

    This is the whole point of a runtime-checkable Protocol here: if a later
    refactor renames one of these verbs, this test fails instead of the TUI
    failing at runtime on one provider only.
    """
    from claude_swap.providers.base import ProviderSwitcher

    assert issubclass(ClaudeAccountSwitcher, ProviderSwitcher)
