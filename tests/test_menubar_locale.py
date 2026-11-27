from datetime import datetime

import pytest

from claude_swap.menubar_locale import weekly_reset_time


@pytest.mark.parametrize("value", [None, "", "invalid", "2026-11-20T20:00:00"])
def test_missing_or_ambiguous_reset(value):
    assert weekly_reset_time("zh-Hans", value) == ""


def test_reset_in_local_timezone():
    value = "2026-11-20T12:00:00+00:00"
    local = datetime.fromisoformat(value).astimezone()
    day = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[local.weekday()]
    assert weekly_reset_time("zh-Hans", value) == f"（{day} {local:%H:%M}）"
    day = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[local.weekday()]
    assert weekly_reset_time("en", value) == f"({day} {local:%H:%M})"
