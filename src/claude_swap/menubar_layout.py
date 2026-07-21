"""Pure geometry planning for the native menu-bar popover.

All coordinates use a top-left origin.  Keeping the layout calculation separate
from AppKit makes the popover's fixed chrome, account-only scrolling, and
long-list behavior directly testable without a macOS runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

POPOVER_WIDTH = 430.0
POPOVER_MAX_HEIGHT = 620.0
HEADER_HEIGHT = 62.0
FOOTER_HEIGHT = 80.0
EMPTY_ACCOUNTS_DOCUMENT_HEIGHT = 48.0
INSET = 14.0
GROUP_GAP = 8.0
BUTTON_HEIGHT = 26.0


@dataclass(frozen=True)
class Rect:
    """An immutable top-down rectangle in popover coordinates."""

    x: float
    y: float
    width: float
    height: float

    @property
    def max_x(self) -> float:
        """Return the rectangle's right edge."""
        return self.x + self.width

    @property
    def max_y(self) -> float:
        """Return the rectangle's bottom edge."""
        return self.y + self.height

    def intersects(self, other: Rect) -> bool:
        """Return whether this rectangle overlaps ``other`` with positive area."""
        return (
            self.x < other.max_x
            and other.x < self.max_x
            and self.y < other.max_y
            and other.y < self.max_y
        )


@dataclass(frozen=True)
class AccountGroupLayout:
    """Frames for one compact account group in the accounts document."""

    frame: Rect
    title: Rect
    capacity: Rect
    email: Rect
    freshness: Rect
    usage_rows: tuple[Rect, ...]
    make_active: Rect
    session: Rect


@dataclass(frozen=True)
class MainPopoverLayout:
    """The fixed main chrome and independently scrolling accounts region."""

    root: Rect
    header: Rect
    title: Rect
    accounts_viewport: Rect
    footer: Rect
    rotate_next: Rect
    switch_best: Rect
    next_available: Rect
    refresh: Rect
    more: Rect
    settings: Rect
    accounts: tuple[AccountGroupLayout, ...]
    accounts_document_height: float


@dataclass(frozen=True)
class ScrollScreenLayout:
    """Top-down scroll document geometry for secondary screens."""

    viewport: Rect
    title: Rect
    subtitle: Rect
    items: tuple[Rect, ...]
    document_height: float


def _button_row(y: float, count: int, width: float) -> tuple[Rect, ...]:
    gap = 8.0
    button_width = (width - 2 * INSET - gap * (count - 1)) / count
    return tuple(
        Rect(INSET + index * (button_width + gap), y, button_width, BUTTON_HEIGHT)
        for index in range(count)
    )


def plan_main_popover(account_row_counts: Sequence[int]) -> MainPopoverLayout:
    """Plan the fixed main popover and its account-only scroll document.

    ``account_row_counts`` contains the already-shaped usage-row count for each
    account. Negative counts are rejected at this boundary rather than producing
    malformed AppKit frames.
    """
    if any(count < 0 for count in account_row_counts):
        raise ValueError("account row counts cannot be negative")

    title = Rect(INSET, 13.0, 300.0, 20.0)

    groups: list[AccountGroupLayout] = []
    y = INSET
    group_width = POPOVER_WIDTH - 2 * INSET
    for row_count in account_row_counts:
        usage_start = 64.0
        actions_y = usage_start + row_count * 20.0 + 4.0
        height = actions_y + BUTTON_HEIGHT + 10.0
        frame = Rect(INSET, y, group_width, height)
        content_x = frame.x + 10.0
        title_frame = Rect(content_x, frame.y + 9.0, 220.0, 18.0)
        capacity = Rect(frame.x + 252.0, frame.y + 9.0, 140.0, 18.0)
        email = Rect(content_x, frame.y + 28.0, 250.0, 18.0)
        freshness = Rect(content_x, frame.y + 46.0, group_width - 20.0, 18.0)
        usage_rows = tuple(
            Rect(content_x, frame.y + usage_start + index * 20.0, group_width - 20.0, 18.0)
            for index in range(row_count)
        )
        make_active, session = _button_row(frame.y + actions_y, 2, POPOVER_WIDTH)
        groups.append(
            AccountGroupLayout(
                frame=frame,
                title=title_frame,
                capacity=capacity,
                email=email,
                freshness=freshness,
                usage_rows=usage_rows,
                make_active=make_active,
                session=session,
            )
        )
        y = frame.max_y + GROUP_GAP

    document_height = max(EMPTY_ACCOUNTS_DOCUMENT_HEIGHT, y - GROUP_GAP + INSET)
    maximum_viewport_height = POPOVER_MAX_HEIGHT - HEADER_HEIGHT - FOOTER_HEIGHT
    viewport_height = min(document_height, maximum_viewport_height)
    root = Rect(0.0, 0.0, POPOVER_WIDTH, HEADER_HEIGHT + viewport_height + FOOTER_HEIGHT)
    header = Rect(0.0, 0.0, POPOVER_WIDTH, HEADER_HEIGHT)
    accounts_viewport = Rect(0.0, header.max_y, POPOVER_WIDTH, viewport_height)
    footer = Rect(0.0, accounts_viewport.max_y, POPOVER_WIDTH, FOOTER_HEIGHT)
    rotate_next, switch_best, next_available = _button_row(footer.y + 10.0, 3, POPOVER_WIDTH)
    refresh, more, settings = _button_row(footer.y + 42.0, 3, POPOVER_WIDTH)
    return MainPopoverLayout(
        root=root,
        header=header,
        title=title,
        accounts_viewport=accounts_viewport,
        footer=footer,
        rotate_next=rotate_next,
        switch_best=switch_best,
        next_available=next_available,
        refresh=refresh,
        more=more,
        settings=settings,
        accounts=tuple(groups),
        accounts_document_height=document_height,
    )


def plan_scroll_screen(item_heights: Sequence[float]) -> ScrollScreenLayout:
    """Plan a full-height secondary scroll document from ordered item heights."""
    if any(height <= 0 for height in item_heights):
        raise ValueError("scroll screen item heights must be positive")

    viewport = Rect(0.0, 0.0, POPOVER_WIDTH, POPOVER_MAX_HEIGHT)
    title = Rect(INSET, 14.0, POPOVER_WIDTH - 2 * INSET, 20.0)
    subtitle = Rect(INSET, 34.0, POPOVER_WIDTH - 2 * INSET, 18.0)
    y = 66.0
    items: list[Rect] = []
    for height in item_heights:
        items.append(Rect(INSET, y, POPOVER_WIDTH - 2 * INSET, height))
        y += height + GROUP_GAP
    document_height = max(POPOVER_MAX_HEIGHT, y - GROUP_GAP + INSET)
    return ScrollScreenLayout(viewport, title, subtitle, tuple(items), document_height)


__all__ = [
    "AccountGroupLayout",
    "BUTTON_HEIGHT",
    "GROUP_GAP",
    "INSET",
    "MainPopoverLayout",
    "POPOVER_MAX_HEIGHT",
    "POPOVER_WIDTH",
    "Rect",
    "ScrollScreenLayout",
    "plan_main_popover",
    "plan_scroll_screen",
]
