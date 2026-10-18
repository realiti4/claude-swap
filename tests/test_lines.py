"""Unit tests for the per-window switch lines (lines.py)."""

from __future__ import annotations

import random

import pytest

from claude_swap import oauth
from claude_swap.lines import BindingWindow, binding_window, line_headroom, window_line


def _usage7(pct5: float, pct7: float) -> dict:
    return {"five_hour": {"pct": pct5}, "seven_day": {"pct": pct7}}


def _binding(pct5: float, pct7: float, threshold: float, weekly=None) -> BindingWindow:
    found = binding_window(oauth.relevant_windows(_usage7(pct5, pct7)), threshold, weekly)
    assert found is not None
    return found


class TestWindowLine:
    def test_5h_always_takes_the_threshold(self):
        assert window_line("5h", 85.0, 97.0) == 85.0

    def test_7d_and_scoped_take_the_weekly_line_when_set(self):
        assert window_line("7d", 85.0, 97.0) == 97.0
        assert window_line("Fable", 85.0, 97.0) == 97.0

    def test_unset_weekly_falls_back_everywhere(self):
        for label in ("5h", "7d", "Fable"):
            assert window_line(label, 85.0, None) == 85.0


class TestLineHeadroom:
    """Three anchors: 0% -> 100, the line -> 100 - T, 100% -> 0."""

    GRID = [(float(t), float(w)) for t in range(50, 100) for w in range(50, 100) if t != w]
    DECIMALS = [
        (t / 10, w / 10)
        for t in range(500, 1000, 7)
        for w in range(500, 1000, 11)
        if t != w
    ]

    def test_equal_lines_are_raw_headroom_bit_for_bit(self):
        for pct in [p / 10 for p in range(0, 1001)]:
            for threshold in (50.0, 85.0, 90.0, 97.5, 99.9):
                assert line_headroom(pct, threshold, threshold) == 100.0 - pct

    def test_anchors_are_exact_on_the_integer_grid(self):
        for threshold, line in self.GRID:
            assert line_headroom(0.0, line, threshold) == 100.0
            assert line_headroom(line, line, threshold) == 100.0 - threshold
            assert line_headroom(100.0, line, threshold) == 0.0

    def test_line_and_exhaustion_anchors_are_exact_for_decimal_settings(self):
        # 0% is within an ulp for non-integer settings (nothing compares
        # there); the line and 100% are where the engine's comparisons sit.
        for threshold, line in self.DECIMALS:
            assert line_headroom(line, line, threshold) == 100.0 - threshold
            assert line_headroom(100.0, line, threshold) == 0.0
            assert line_headroom(0.0, line, threshold) == pytest.approx(100.0)

    def test_values_under_and_over_the_weekly_line(self):
        assert line_headroom(93.0, 97.0, 85.0) == pytest.approx(15 + 85 * 4 / 97)
        assert line_headroom(98.0, 97.0, 85.0) == 10.0
        assert line_headroom(99.0, 97.0, 85.0) == 5.0

    def test_a_weekly_line_below_the_5h_line(self):
        # T90/W80: a weekly point under the line is worth more than a 5h one,
        # and the over-line band still descends to 0 at exhaustion.
        assert line_headroom(70.0, 80.0, 90.0) == pytest.approx(10 + 90 * 10 / 80)
        assert line_headroom(85.0, 80.0, 90.0) == 7.5
        assert line_headroom(100.0, 80.0, 90.0) == 0.0

    def test_the_exact_boundary_that_a_rescale_missed(self):
        # T92/W86: (line - pct) * (T / line) left 8.000000000000014 here, and
        # a 5h candidate exactly `hysteresis` better read 9.99999... < 10.
        assert line_headroom(86.0, 86.0, 92.0) == 8.0
        assert line_headroom(82.0, 92.0, 92.0) == 18.0

    def test_monotone_in_each_windows_pct(self):
        rng = random.Random(7)
        for _ in range(20000):
            threshold = rng.choice([50.0, 85.0, 90.0, 97.5])
            weekly = rng.choice([50.0, 80.0, 97.0, 99.9])
            pct5, pct7 = rng.uniform(0, 100), rng.uniform(0, 100)
            step = rng.uniform(0, 5)
            before = _binding(pct5, pct7, threshold, weekly).line_headroom
            up5 = _binding(min(pct5 + step, 100), pct7, threshold, weekly).line_headroom
            up7 = _binding(pct5, min(pct7 + step, 100), threshold, weekly).line_headroom
            assert up5 <= before + 1e-12 and up7 <= before + 1e-12


class TestBindingWindow:
    def test_least_policy_headroom_binds_not_the_highest_pct(self):
        found = _binding(5.0, 93.0, 85.0, 97.0)
        assert (found.label, found.pct, found.line, found.over) == ("7d", 93.0, 97.0, False)
        found = _binding(95.0, 47.0, 85.0, 97.0)
        assert (found.label, found.over) == ("5h", True)

    def test_over_is_the_direct_comparison_at_the_line(self):
        assert _binding(5.0, 97.0, 85.0, 97.0).over is True
        assert _binding(5.0, 96.9, 85.0, 97.0).over is False
        assert _binding(85.0, 5.0, 85.0, 97.0).over is True

    def test_ties_keep_the_first_window(self):
        assert _binding(0.0, 0.0, 90.0).label == "5h"
        assert _binding(50.0, 50.0, 90.0, 90.0).label == "5h"

    def test_no_windows_gives_none(self):
        assert binding_window([], 85.0, 97.0) is None
        assert binding_window(oauth.relevant_windows(None), 85.0) is None

    def test_resets_at_travels_with_the_window(self):
        usage = {
            "five_hour": {"pct": 90.0, "resets_at": "2026-01-01T01:00:00Z"},
            "seven_day": {"pct": 93.0, "resets_at": "2026-01-05T00:00:00Z"},
        }
        found = binding_window(oauth.relevant_windows(usage), 85.0, 97.0)
        assert (found.label, found.resets_at) == ("5h", "2026-01-01T01:00:00Z")

    def test_scoped_windows_take_the_weekly_line(self):
        usage = {
            "five_hour": {"pct": 5.0},
            "seven_day": {"pct": 10.0},
            "scoped": [{"name": "Fable", "pct": 96.0}],
        }
        found = binding_window(oauth.relevant_windows(usage, ("Fable",)), 85.0, 97.0)
        assert (found.label, found.line, found.over) == ("Fable", 97.0, False)
        ignored = binding_window(oauth.relevant_windows(usage, ()), 85.0, 97.0)
        assert ignored.label == "7d"

    def test_weekly_unset_reproduces_the_max_pct_rule_exactly(self):
        rng = random.Random(0)
        for _ in range(20000):
            threshold = rng.choice([50.0, 85.0, 90.0, 97.5, 99.9])
            pct5 = round(rng.uniform(0, 100), 1)
            pct7 = round(rng.uniform(0, 100), 1)
            found = _binding(pct5, pct7, threshold)
            assert found.pct == max(pct5, pct7)
            assert found.label == ("5h" if pct5 >= pct7 else "7d")
            assert found.line_headroom == oauth.account_headroom(_usage7(pct5, pct7))
            assert found.over == (max(pct5, pct7) >= threshold)

    def test_over_matches_any_window_and_headroom_matches_the_minimum(self):
        rng = random.Random(1)
        for _ in range(20000):
            threshold = rng.choice([50.0, 85.0, 90.0, 97.5])
            weekly = rng.choice([None, 80.0, 97.0, 99.9])
            pct5 = round(rng.uniform(0, 100), 1)
            pct7 = round(rng.uniform(0, 100), 1)
            found = _binding(pct5, pct7, threshold, weekly)
            line7 = window_line("7d", threshold, weekly)
            assert found.over == (pct5 >= threshold or pct7 >= line7)
            assert found.line_headroom == min(
                line_headroom(pct5, threshold, threshold),
                line_headroom(pct7, line7, threshold),
            )

    def test_the_binding_flip_is_continuous(self):
        # Choosing the window by one score and ranking by another made 0.1
        # more usage look healthier here (5.0 -> 13.2 policy points).
        assert _binding(86.7, 99.0, 85.0, 97.0).line_headroom == 5.0
        assert _binding(86.8, 99.0, 85.0, 97.0).line_headroom == 5.0


class TestBindingWindowScale:
    """``scale`` separates an account's own line from the ruler its room is
    measured on, so accounts on different lines are scored on one scale.

    The engine passes each account's effective threshold as the line and the
    global threshold as the scale; without ``scale`` the line is its own ruler,
    exactly as before.
    """

    WINDOW = [("5h", 50.0, "2024-01-05T00:00:00Z")]

    def test_a_scale_scores_the_room_on_that_ruler(self):
        found = binding_window(self.WINDOW, 60.0, scale=90.0)
        assert found.line_headroom == 25.0
        assert found.line_headroom == line_headroom(50.0, 60.0, 90.0)

    def test_no_scale_is_the_line_as_its_own_ruler(self):
        assert binding_window(self.WINDOW, 60.0).line_headroom == 50.0

    def test_a_scale_equal_to_the_line_is_plain_headroom(self):
        assert binding_window(self.WINDOW, 60.0, scale=60.0).line_headroom == 50.0

    def test_scale_moves_only_the_room(self):
        plain = binding_window(self.WINDOW, 60.0)
        for scale in (50.0, 85.0, 90.0, 95.0):
            found = binding_window(self.WINDOW, 60.0, scale=scale)
            assert (found.label, found.pct, found.line, found.resets_at, found.over) == (
                plain.label, plain.pct, plain.line, plain.resets_at, plain.over
            )

    def test_scale_is_keyword_only(self):
        with pytest.raises(TypeError):
            binding_window(self.WINDOW, 60.0, None, 90.0)

    def test_the_scale_can_change_which_window_binds(self):
        # 5h 73.3% on a 55.5 line and 7d 94% on a 90 weekly line tie at 6.0 on
        # the 90 scale (first window wins the tie); on the 55.5 ruler the 7d
        # window has 26.7 against the 5h window's 26.700000000000003. Every
        # site that asks which window binds must pass the same scale.
        windows = list(oauth.relevant_windows(_usage7(73.3, 94.0)))
        assert binding_window(windows, 55.5, 90.0, scale=90.0).label == "5h"
        assert binding_window(windows, 55.5, 90.0).label == "7d"
