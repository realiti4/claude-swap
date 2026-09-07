"""Codex plan-tier naming.

codex-auth's schema v4 renamed the tiers to final product semantics. Records
imported from an older registry — and the ``plan_type`` the usage API returns —
are normalized through here so one Business account never displays as
Enterprise in one view and Business in another.
"""

from __future__ import annotations

#: v4's renames, applied as ONE lookup. Applied sequentially, a legacy ``team``
#: would pass through ``business`` and land on ``enterprise`` — every Business
#: account mislabelled one tier up.
_RENAMES = {"team": "business", "business": "enterprise"}


def normalize_plan(plan: object) -> str:
    """Map a stored or reported plan tier onto v4's final semantics."""
    if not isinstance(plan, str) or not plan:
        return ""
    return _RENAMES.get(plan, plan)
