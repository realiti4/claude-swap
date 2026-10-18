"""TUI policy display.

The per-account policy set through ``cswap threshold`` has
to be visible where the operator already looks, on both account render paths:

* ``account_card_text`` — the expanded card for the selected account.
* ``mini_account_text`` — the one-line minimized row for every other account.

Both render paths show the threshold override on the account row, and an
account carrying no policy renders byte-identically to before, on both paths —
asserted against goldens captured from the pre-change engine. The auto screen's
"Next best" list ranks each account against its own line, as the engine does.

This module is display-only. Editing a policy from inside the TUI is out of
scope for this PR; these tests must not grow an input path.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from claude_swap.models import AccountPolicy
from claude_swap.tui.widgets import account_card_text, mini_account_text

from tests.test_tui import (
    FakeSwitcher,
    fake_engine,  # noqa: F401 - the auto screen's engine fixture, used by name
    make_account,
    make_app,
    make_entry,
    settle,
)

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


class TestThresholdBadge:
    """A per-account override is legible on both paths."""

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
            make_account(2, active=True, alias="dev"), threshold=60.0
        )
        header = card(acc).splitlines()[0]
        assert "60%" in header
        row = mini(acc)
        assert "60%" in row


class TestUnpolicedAccountsRenderExactlyAsBefore:
    """The negative half, and the reason this file has goldens.

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
        for token in ("th ",):
            assert token not in "\n".join(lines[1:])


@pytest.mark.asyncio
class TestNextBestRanksEachAccountOnItsOwnLine:
    """The auto screen's "Next best" list ranks with the engine's own key:
    room under each account's own line, measured on the global threshold.

    Global 90, no weekly line. Account 2 is at 5h 50% under a 60% line of its
    own: 10 + 90 * 10 / 60 = 25 points. Account 3 is at 5h 55% under the
    global line: 45 points. Raw headroom (50 vs 45) would put account 2 first.
    """

    async def _open(self, pilot):
        await settle(pilot)
        await pilot.press("g")
        await pilot.pause()

    def _fleet(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({
            "schemaVersion": 1,
            "autoswitch": {"threshold": 90},
        }))
        return FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(91.0, 20.0)),
                with_policy(
                    make_account(2, entry=make_entry(50.0, 10.0)), threshold=60.0
                ),
                make_account(3, entry=make_entry(55.0, 10.0)),
            ],
            tmp_path,
        )

    @staticmethod
    def _row(plain: str, email: str) -> str:
        return next(line for line in plain.splitlines() if email in line)

    async def test_room_under_the_accounts_own_line_orders_the_list(
        self, tmp_path, fake_engine
    ):
        app = make_app(self._fleet(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            from textual.widgets import Static

            plain = app.screen.query_one("#candidates", Static).render().plain
            assert plain.index("user3@example.com") < plain.index(
                "user2@example.com"
            )
            assert "5h  50% used (line 60%)" in self._row(plain, "user2@example.com")
            assert "(line" not in self._row(plain, "user3@example.com")

    async def test_a_session_adjust_moves_only_the_global_line(
        self, tmp_path, fake_engine
    ):
        # At 93 account 2 scores 7 + 93 * 10 / 60 = 22.5 and account 3 45:
        # the order holds, and account 2 is still judged against its own 60%.
        app = make_app(self._fleet(tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await self._open(pilot)
            await settle(pilot)
            from textual.widgets import Static

            await pilot.press("t", "right", "right", "right")
            await pilot.pause()
            assert app.screen._settings.threshold == 93.0
            plain = app.screen.query_one("#candidates", Static).render().plain
            assert plain.index("user3@example.com") < plain.index(
                "user2@example.com"
            )
            assert "5h  50% used (line 60%)" in self._row(plain, "user2@example.com")
