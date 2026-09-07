"""Plan-tier naming, normalized to codex-auth v4 semantics."""

from __future__ import annotations

from claude_swap.codex.plans import normalize_plan


def test_legacy_team_becomes_business():
    assert normalize_plan("team") == "business"


def test_legacy_business_becomes_enterprise():
    assert normalize_plan("business") == "enterprise"


def test_the_rename_is_a_single_lookup_not_two_passes():
    """Applied sequentially, 'team' would pass through 'business' and land on
    'enterprise' — every Business account mislabelled one tier up."""
    assert normalize_plan("team") != "enterprise"


def test_unrenamed_tiers_pass_through():
    assert normalize_plan("pro") == "pro"
    assert normalize_plan("plus") == "plus"
    assert normalize_plan("enterprise") == "enterprise"


def test_missing_or_non_string_plans_become_empty():
    assert normalize_plan(None) == ""
    assert normalize_plan("") == ""
    assert normalize_plan(3) == ""
