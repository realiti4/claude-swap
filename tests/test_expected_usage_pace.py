from datetime import datetime, timezone

import pytest

from claude_swap.tui.widgets import (
    _expected_for_row,
    bar_cells,
    usage_bar,
)

NOW = 1_700_000_000.0
HOUR = 3600
DAY = 86400


def window(pct, reset):
    return {
        "pct": pct,
        "resets_at": datetime.fromtimestamp(
            reset, timezone.utc
        ).isoformat(),
    }


def test_five_hour_expected_pace():
    usage = {
        "five_hour": window(27, NOW + HOUR)
    }
    assert _expected_for_row(usage, "5h", NOW) == pytest.approx(80)


def test_weekly_expected_pace():
    usage = {
        "seven_day": window(90, NOW + 2 * DAY)
    }
    assert _expected_for_row(usage, "7d", NOW) == pytest.approx(
        5 / 7 * 100
    )


def test_scoped_weekly_expected_pace():
    usage = {
        "scoped": [
            {"name": "Fable", **window(40, NOW + 3 * DAY)}
        ]
    }
    assert _expected_for_row(
        usage, "Fable", NOW
    ) == pytest.approx(4 / 7 * 100)


def test_missing_reset_is_safe():
    assert _expected_for_row(
        {"five_hour": {"pct": 30}}, "5h", NOW
    ) is None


def test_spend_has_no_expected_pace():
    assert _expected_for_row({}, "$$", NOW) is None


def test_expected_marker_position():
    bar = bar_cells(25, 20, expected_pct=50).plain
    assert bar[10] == "│"


def test_threshold_takes_priority():
    bar = bar_cells(
        25, 20, threshold=50, expected_pct=50
    ).plain
    assert bar[10] == "┃"


def test_expected_percentage_displayed():
    result = usage_bar(
        "7d", 25, None, 20, expected_pct=50
    ).plain
    assert "25%" in result
    assert "exp 50%" in result
