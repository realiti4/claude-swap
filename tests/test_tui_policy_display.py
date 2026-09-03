"""TUI policy display — MEU-PAP-05 (AC-39, AC-40).

The per-account policy set through ``cswap threshold`` / ``cswap backup`` has
to be visible where the operator already looks, on both account render paths:

* ``account_card_text`` — the expanded card for the selected account.
* ``mini_account_text`` — the one-line minimized row for every other account.

AC-39  Both render paths show ``(backup)`` and the threshold override on the
       account row.
AC-40  An account carrying no policy renders byte-identically to today, on both
       paths — asserted against goldens captured from the pre-change engine.

This module is display-only. Editing a policy from inside the TUI is out of
scope for this PR (plan §Scope, Deferred D-2); these tests must not grow an
input path.
"""

from __future__ import annotations

import dataclasses

import pytest

from claude_swap.models import AccountPolicy
from claude_swap.tui.widgets import account_card_text, mini_account_text

from tests.test_tui import make_account

# Goldens captured from the unmodified engine at
# 4f6c6ac6f303bd910d51bf5bf2fb36d553f4f37f60a50e438291d4feb5cadfbe, rendered
# through the same ``make_account`` fixture used below.  Only the header row and
# the whole mini row are pinned as literals: the card's bar rows carry a wall
# clock date and would rot daily.  Their invariance is asserted structurally
# instead (see ``test_the_card_bar_rows_are_untouched``).
GOLDEN_CARD_HEADER = " 2  dev (user2@example.com)  [personal]   ● active"
GOLDEN_MINI = " 2  dev (user2@example.com)  [personal]   5h 25% · 7d 10%"
GOLDEN_CARD_HEADER_DISABLED = " 3  user3@example.com  [personal]   (disabled)"
GOLDEN_MINI_DISABLED = " 3  user3@example.com  [personal]  (disabled)   5h 25% · 7d 10%"

NOW = 1000.0


def with_policy(acc, **kwargs):
    """Attach an ``AccountPolicy`` to a snapshot from ``tests.test_tui``.

    ``make_account`` predates the policy field and takes no ``policy=``
    argument; ``test_tui.py`` is upstream's and stays byte-unchanged, so the
    snapshot is rebuilt here instead of the helper being widened.
    """
    return dataclasses.replace(acc, policy=AccountPolicy(**kwargs))


def card(acc) -> str:
    return account_card_text(acc, 80, now=NOW).plain


def mini(acc) -> str:
    return mini_account_text(acc, NOW).plain


class TestBackupBadge:
    """AC-39, first half — the reserve is legible on both paths."""

    def test_the_card_marks_a_backup_account(self):
        acc = with_policy(make_account(2, active=True, alias="dev"), backup=True)
        assert "(backup)" in card(acc).splitlines()[0]

    def test_the_mini_row_marks_a_backup_account(self):
        acc = with_policy(make_account(2, active=True, alias="dev"), backup=True)
        assert "(backup)" in mini(acc)

    def test_a_primary_account_carries_no_backup_badge(self):
        acc = with_policy(make_account(2, active=True, alias="dev"), backup=False)
        assert "(backup)" not in card(acc)
        assert "(backup)" not in mini(acc)

    def test_the_badge_survives_alongside_disabled(self):
        # A reserve can also be disabled; neither badge may swallow the other.
        acc = with_policy(make_account(3, disabled=True), backup=True)
        header = card(acc).splitlines()[0]
        assert "(disabled)" in header and "(backup)" in header
        row = mini(acc)
        assert "(disabled)" in row and "(backup)" in row


class TestThresholdBadge:
    """AC-39, second half — a per-account override is legible on both paths."""

    def test_the_card_shows_the_override(self):
        acc = with_policy(make_account(2, active=True, alias="dev"), threshold=75.0)
        assert "75%" in card(acc).splitlines()[0]

    def test_the_mini_row_shows_the_override(self):
        acc = with_policy(make_account(2, active=True, alias="dev"), threshold=75.0)
        assert "75%" in mini(acc)

    @pytest.mark.parametrize(
        ("value", "shown"),
        [(50.0, "50%"), (75.0, "75%"), (82.5, "82.5%"), (99.9, "99.9%")],
    )
    def test_a_fractional_override_keeps_its_fraction_and_a_whole_one_stays_whole(
        self, value, shown
    ):
        # 75.0 must not read as "75.0%" and 82.5 must not round to "82%": the
        # store accepts one decimal place (ACCOUNT_THRESHOLD_MIN..MAX) and the
        # row has to show back exactly what was set.
        acc = with_policy(make_account(2, active=True, alias="dev"), threshold=value)
        assert shown in card(acc).splitlines()[0]
        assert shown in mini(acc)

    def test_an_account_on_the_global_default_shows_no_override(self):
        acc = with_policy(make_account(2, active=True, alias="dev"))
        assert acc.policy.threshold is None
        assert "%" not in card(acc).splitlines()[0]
        assert "th " not in mini(acc)

    def test_both_badges_render_together(self):
        acc = with_policy(
            make_account(2, active=True, alias="dev"), threshold=60.0, backup=True
        )
        header = card(acc).splitlines()[0]
        assert "(backup)" in header and "60%" in header
        row = mini(acc)
        assert "(backup)" in row and "60%" in row


class TestUnpolicedAccountsRenderExactlyAsBefore:
    """AC-40 — the negative half, and the reason this file has goldens.

    Every account in an untouched store carries ``AccountPolicy()``.  If the
    badge code path leaks so much as a stray space onto those rows, the whole
    fleet's display changes for a feature nobody opted into.
    """

    def test_the_card_header_is_byte_identical_to_the_golden(self):
        assert card(make_account(2, active=True, alias="dev")).splitlines()[0] == (
            GOLDEN_CARD_HEADER
        )

    def test_the_mini_row_is_byte_identical_to_the_golden(self):
        assert mini(make_account(2, active=True, alias="dev")) == GOLDEN_MINI

    def test_a_disabled_account_is_byte_identical_to_the_golden(self):
        acc = make_account(3, disabled=True)
        assert card(acc).splitlines()[0] == GOLDEN_CARD_HEADER_DISABLED
        assert mini(acc) == GOLDEN_MINI_DISABLED

    def test_an_explicit_empty_policy_renders_the_same_as_no_policy(self):
        # ``AccountPolicy()`` is the field default, but a store that wrote an
        # empty policy object back must not be distinguishable on screen.
        plain = make_account(2, active=True, alias="dev")
        assert card(with_policy(plain)) == card(plain)
        assert mini(with_policy(plain)) == mini(plain)

    def test_the_card_bar_rows_are_untouched(self):
        # The bar rows carry a wall-clock reset date, so they are pinned
        # structurally rather than as a literal: same row count, same window
        # labels, and no policy token anywhere below the header.
        acc = make_account(2, active=True, alias="dev")
        lines = card(acc).splitlines()
        assert len(lines) == 3
        assert lines[1].startswith("    5h ")
        assert lines[2].startswith("    7d ")
        for token in ("(backup)", "th "):
            assert token not in "\n".join(lines[1:])
