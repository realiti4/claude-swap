"""Pure fixture/view-model tests for the isolated AppKit prototype."""

from __future__ import annotations

import unittest

from prototypes import modern_menubar
from prototypes.modern_menubar import (
    CapacityState,
    FreshnessState,
    fixture_accounts,
    fixture_view_models,
)


def test_fixture_accounts_cover_scroll_and_requested_states() -> None:
    accounts = fixture_accounts()

    assert len(accounts) >= 8
    assert sum(account.is_active for account in accounts) == 1
    assert any(account.is_disabled for account in accounts)
    assert any(account.freshness == FreshnessState.STALE for account in accounts)
    assert any(account.freshness == FreshnessState.UNAVAILABLE for account in accounts)


def test_each_fixture_account_has_all_comparison_scopes() -> None:
    for account in fixture_accounts():
        assert [usage.label for usage in account.usage] == ["5h", "Weekly", "Fable"]


def test_view_models_shape_percent_capacity_and_unavailable_rows() -> None:
    models = fixture_view_models()
    active = next(model for model in models if model.is_active)
    unavailable = next(model for model in models if model.freshness == FreshnessState.UNAVAILABLE)

    assert active.capacity_summary == "43% minimum capacity"
    assert active.rows[0].used_percent == 42
    assert active.rows[0].available_percent == 58
    assert active.rows[0].state == CapacityState.AVAILABLE
    assert unavailable.capacity_summary == "Capacity unavailable"
    assert all(row.state == CapacityState.UNAVAILABLE for row in unavailable.rows)
    assert all(row.available_percent is None for row in unavailable.rows)


def test_high_usage_rows_are_explicitly_labelled() -> None:
    models = fixture_view_models()
    research = next(model for model in models if model.alias == "research")
    agency = next(model for model in models if model.alias == "agency")

    assert research.rows[-1].state == CapacityState.LIMIT_REACHED
    assert research.rows[-1].state_label == "Limit reached"
    assert agency.rows[0].state == CapacityState.NEAR_LIMIT
    assert agency.rows[-1].state_label == "Limit reached"


def test_capacity_state_threshold_boundaries() -> None:
    assert modern_menubar.capacity_state(69) == CapacityState.AVAILABLE
    assert modern_menubar.capacity_state(70) == CapacityState.NEAR_LIMIT
    assert modern_menubar.capacity_state(89) == CapacityState.NEAR_LIMIT
    assert modern_menubar.capacity_state(90) == CapacityState.LIMIT_REACHED


@unittest.skipUnless(modern_menubar.APPKIT_AVAILABLE, "requires macOS PyObjC/AppKit")
def test_macos_appkit_smoke_constructs_status_item_and_popover() -> None:
    """Construct native objects without entering the application event loop."""

    delegate = modern_menubar.setup_application()
    assert modern_menubar.setup_application() is delegate
    delegate.applicationDidFinishLaunching_(None)
    try:
        assert modern_menubar._app_delegate is delegate
        assert delegate.status_item.length() == modern_menubar.STATUS_ITEM_LENGTH
        button = delegate.status_item.button()
        assert button is not None
        assert button.image().isTemplate()
        assert button.imageScaling() == modern_menubar.NSImageScaleProportionallyDown
        assert delegate.popover.contentViewController() is delegate.controller
        status_item = delegate.status_item
        delegate.applicationDidFinishLaunching_(None)
        assert delegate.status_item is status_item
    finally:
        modern_menubar.NSStatusBar.systemStatusBar().removeStatusItem_(delegate.status_item)
        modern_menubar._app_delegate = None
