"""Refresh Business/Enterprise/Edu workspace names.

A personal ChatGPT account has no workspace name; a Business, Enterprise or Edu
one does, and it is what tells two of a user's workspaces apart in a listing.
The name is not in the token — it only comes from ``/backend-api/accounts``.

The point of this module is **not** fetching, it is *not* fetching. Asking for
workspace names on every listing would add a request per user scope to every
``cswap codex list``, forever, to fill a field that changes approximately never.
So codex-auth's grouped-scope rules (its ``docs/api.md``) are followed exactly:

- A scope is all stored slots sharing one ``chatgpt_user_id``. One user can hold
  several workspaces; one request answers for all of them.
- A request is attempted only when the scope holds **more than one** record, at
  least one of them is a workspace plan, and at least one such record still has
  no name. A single personal account therefore never triggers a request.
- At most one request per scope per pass.
- Matched records overwrite the stored name even when they already had one — the
  server is authoritative, and a renamed workspace should follow.
- In-scope records the response does *not* return are cleared back to empty:
  leaving a stale name for a workspace the user has lost access to is worse than
  showing none.
- Any failure is non-fatal and leaves stored names untouched. A missing
  workspace name is cosmetic; it must never fail a listing.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from claude_swap.codex.store import CodexSlot, CodexStore
from claude_swap.codex.usage import fetch_workspace_names

_logger = logging.getLogger(__name__)

#: Plans that have a workspace name worth fetching. A personal plan never does.
WORKSPACE_PLANS = frozenset({"business", "enterprise", "edu"})


def _user_id(slot: CodexSlot) -> str:
    return slot.account_key.partition("::")[0]


def _account_id(slot: CodexSlot) -> str:
    return slot.account_key.rpartition("::")[2]


def scope_needs_refresh(scope: list[CodexSlot]) -> bool:
    """Whether this ``chatgpt_user_id`` scope justifies a request."""
    if len(scope) <= 1:
        return False
    workspace_slots = [s for s in scope if s.plan in WORKSPACE_PLANS]
    if not workspace_slots:
        return False
    return any(not s.workspace_name for s in workspace_slots)


def refresh_workspace_names(
    store: CodexStore,
    *,
    payload_for,
) -> int:
    """Fill in missing workspace names. Returns how many names changed.

    ``payload_for`` is the same callback the usage cache takes, so this obeys
    the never-refresh-the-active-account rule without knowing about it.
    """
    scopes: dict[str, list[CodexSlot]] = defaultdict(list)
    for slot in store.slots():
        if slot.auth_mode == "apikey":
            continue
        scopes[_user_id(slot)].append(slot)

    changed = 0
    for scope in scopes.values():
        if not scope_needs_refresh(scope):
            continue

        # One request per scope. Any slot in it can ask on the scope's behalf,
        # so use the first with usable credentials.
        names: dict[str, str] = {}
        for slot in scope:
            payload = payload_for(slot)
            tokens = (payload or {}).get("tokens") or {}
            token, account_id = tokens.get("access_token"), tokens.get("account_id")
            if not token or not account_id:
                continue
            names = fetch_workspace_names(token, account_id)
            break

        if not names:
            # Includes the failure case: leave every stored name untouched.
            continue

        for slot in scope:
            returned = names.get(_account_id(slot))
            if returned is not None:
                if returned != slot.workspace_name:
                    store.set_workspace_name(slot.account_key, returned)
                    changed += 1
            elif slot.workspace_name and slot.plan in WORKSPACE_PLANS:
                # In scope, a workspace plan, and not returned: access is gone.
                store.set_workspace_name(slot.account_key, "")
                changed += 1

    return changed
