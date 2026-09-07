"""Which providers this installation actually has accounts for.

Claude is always present — cswap is its switcher and the existing surfaces have
always assumed it. Codex appears only once the user has Codex accounts, so a
Claude-only user's dashboard, menu bar and auto loop look and behave exactly as
they did before: no empty section, no wasted work, nothing new to learn.

That "only when it exists" rule is the whole reason this module is separate from
the provider implementations. It is the one place that decides whether a surface
is single- or multi-provider, so no surface has to make that judgement itself.
"""

from __future__ import annotations

import logging

from claude_swap.providers.base import ProviderSwitcher

_logger = logging.getLogger(__name__)


def codex_is_present() -> bool:
    """Whether this machine has any Codex accounts cswap knows or could import.

    Counts an un-imported codex-auth registry too, so the provider shows up on
    the first run rather than only after a ``cswap codex`` command.
    """
    try:
        from claude_swap.codex import paths as cpaths
        from claude_swap.codex.store import CodexStore

        if CodexStore().slots():
            return True
        return cpaths.get_codex_auth_registry_path().exists()
    except Exception as e:  # pragma: no cover - defensive
        _logger.debug("codex presence check failed: %s", type(e).__name__)
        return False


def available_providers(claude: ProviderSwitcher) -> list[ProviderSwitcher]:
    """Every provider worth showing, Claude first.

    ``claude`` is passed in rather than constructed here: building a
    ``ClaudeAccountSwitcher`` runs migrations and touches the account store, and
    the callers that need this already hold one.
    """
    providers: list[ProviderSwitcher] = [claude]
    if codex_is_present():
        try:
            from claude_swap.codex.switcher import CodexSwitcher

            providers.append(CodexSwitcher())
        except Exception as e:  # pragma: no cover - defensive
            # A broken Codex store must never take the Claude dashboard down.
            _logger.warning("Codex provider unavailable: %s", type(e).__name__)
    return providers
