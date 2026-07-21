"""Pure geometry contracts for the AppKit menu-bar popover."""

from __future__ import annotations

import pytest

from claude_swap.menubar_layout import (
    BUTTON_HEIGHT,
    MainPopoverLayout,
    POPOVER_MAX_HEIGHT,
    Rect,
    plan_main_popover,
    plan_scroll_screen,
)


def _assert_disjoint(frames: tuple[Rect, ...]) -> None:
    for index, frame in enumerate(frames):
        for other in frames[index + 1 :]:
            assert not frame.intersects(other)


def _assert_main_chrome(layout: MainPopoverLayout) -> None:
    _assert_disjoint((layout.header, layout.accounts_viewport, layout.footer))
    assert layout.accounts_viewport.y == layout.header.max_y
    assert layout.footer.y == layout.accounts_viewport.max_y
    assert layout.accounts_document_height >= layout.accounts_viewport.height


def _assert_contained(child: Rect, parent: Rect) -> None:
    assert parent.x <= child.x
    assert parent.y <= child.y
    assert child.max_x <= parent.max_x
    assert child.max_y <= parent.max_y


def test_main_layout_handles_no_accounts_without_a_tall_artificial_document() -> None:
    layout = plan_main_popover(())

    _assert_main_chrome(layout)
    assert layout.accounts == ()
    assert layout.root.height == 190.0
    assert layout.root.height < POPOVER_MAX_HEIGHT
    assert layout.accounts_document_height == layout.accounts_viewport.height == 48.0


def test_main_layout_places_one_account_actions_below_its_usage_rows() -> None:
    layout = plan_main_popover((3,))
    account = layout.accounts[0]

    _assert_main_chrome(layout)
    assert account.usage_rows[-1].max_y <= account.make_active.y
    assert account.usage_rows[-1].max_y <= account.session.y
    assert account.make_active.height == BUTTON_HEIGHT
    assert account.make_active.width >= 150
    assert account.session.width >= 150
    assert account.frame.y >= 0
    assert account.frame.max_y <= layout.accounts_document_height


def test_main_layout_keeps_two_accounts_non_overlapping() -> None:
    layout = plan_main_popover((0, 2))
    first, second = layout.accounts

    _assert_main_chrome(layout)
    assert first.frame.max_y < second.frame.y
    assert layout.root.height < POPOVER_MAX_HEIGHT
    assert layout.accounts_document_height == layout.accounts_viewport.height
    assert second.frame.max_y + 14.0 == layout.accounts_document_height
    for account in (first, second):
        children = (
            account.title,
            account.capacity,
            account.email,
            account.freshness,
            *account.usage_rows,
            account.make_active,
            account.session,
        )
        _assert_disjoint(children)
        for child in children:
            _assert_contained(child, account.frame)


def test_main_layout_grows_only_the_accounts_document_for_long_mixed_lists() -> None:
    layout = plan_main_popover((0, 1, 4, 2, 5, 3, 0, 6, 1, 4))

    _assert_main_chrome(layout)
    assert layout.root.height == POPOVER_MAX_HEIGHT
    assert layout.accounts_document_height > layout.accounts_viewport.height
    assert all(
        account.frame.max_y <= layout.accounts_document_height for account in layout.accounts
    )
    _assert_disjoint(tuple(account.frame for account in layout.accounts))
    assert layout.accounts[0].frame.y == 14.0
    assert layout.accounts[-1].frame.max_y + 14.0 == layout.accounts_document_height


def test_main_layout_has_exactly_two_compact_footer_rows_with_readable_titles_space() -> None:
    layout = plan_main_popover((1,))
    first_row = (layout.rotate_next, layout.switch_best, layout.next_available)
    second_row = (layout.refresh, layout.more, layout.settings)

    assert {frame.y for frame in first_row} != {frame.y for frame in second_row}
    assert all(frame.width >= 120 for frame in (*first_row, *second_row))
    _assert_disjoint((*first_row, *second_row))
    assert all(layout.footer.y <= frame.y and frame.max_y <= layout.footer.max_y for frame in (*first_row, *second_row))


def test_secondary_scroll_planner_stays_top_down_for_long_content() -> None:
    layout = plan_scroll_screen((22.0,) * 50)

    assert layout.title.y == 14.0
    assert layout.subtitle.y > layout.title.y
    assert layout.items[0].y > layout.subtitle.max_y
    assert all(item.y >= 0 for item in layout.items)
    assert layout.items[-1].max_y + 14.0 == layout.document_height
    assert layout.document_height > layout.viewport.height


@pytest.mark.parametrize("planner, values", [(plan_main_popover, (-1,)), (plan_scroll_screen, (0.0,))])
def test_planners_reject_invalid_item_sizes(planner, values) -> None:
    with pytest.raises(ValueError):
        planner(values)
