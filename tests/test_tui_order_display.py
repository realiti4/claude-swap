"""TUI order display and scope — MEU-ORD-05 (AC-39 … AC-42).

AC-39  `_policy_badges` appends `ord R` when the account is pinned, on both
       render paths, and an unpinned account renders byte-identically to its
       PR 1 render.
AC-40  Badge order on the row is deterministic: `(backup)`, `th NN%`, `ord R`.
AC-41  The TUI renders MEU-ORD-04's `failback-hold` reason wherever it already
       renders `NoSwitchEvent.reason`, and a fleet with no reserve still
       renders `below-threshold` exactly as before.
AC-42  **No TUI editing.** No new keybinding, modal, or screen; authoring stays
       CLI-only. `modals.py` and `dashboard.py` are not modified.

The goldens below are the PR 1 renders, taken from `test_tui_policy_display.py`
verbatim rather than recaptured. Recapturing them from the *current* tree would
make them tautological — a badge accidentally emitted for an unpinned account
would be baked into its own oracle.

This module is display-only. It must not grow an input path; that is what AC-42
is for.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from claude_swap.autoswitch import NoSwitchEvent
from claude_swap.models import AccountPolicy
from claude_swap.tui.autoview import event_text
from claude_swap.tui.widgets import account_card_text, mini_account_text

from tests.test_tui import make_account
from tests.test_tui_policy_display import (
    GOLDEN_CARD_HEADER,
    GOLDEN_MINI,
    NOW,
    card,
    mini,
    with_policy,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "claude_swap"


class TestOrderBadge:
    """AC-39, first half — the pin is legible on both render paths."""

    def test_the_card_shows_the_rank(self):
        acc = with_policy(make_account(2, active=True, alias="dev"), order=3)
        assert "ord 3" in card(acc).splitlines()[0]

    def test_the_mini_row_shows_the_rank(self):
        acc = with_policy(make_account(2, active=True, alias="dev"), order=3)
        assert "ord 3" in mini(acc)

    def test_an_unpinned_account_carries_no_badge(self):
        acc = with_policy(make_account(2, active=True, alias="dev"), order=None)
        assert "ord" not in card(acc)
        assert "ord" not in mini(acc)

    def test_the_boundary_ranks_render(self):
        for rank in (1, 999):
            acc = with_policy(make_account(2, active=True, alias="dev"), order=rank)
            assert f"ord {rank}" in mini(acc)

    def test_each_path_uses_its_own_gap(self):
        """The card separates badges with three spaces, the mini row with two.

        `_policy_badges` takes the gap as a parameter for exactly this reason;
        a hard-coded separator in the new branch would pass every `in` check
        above and still misalign one of the two rows.
        """
        acc = with_policy(make_account(2, active=True, alias="dev"), order=3)
        assert "   ord 3" in card(acc).splitlines()[0]
        assert "  ord 3" in mini(acc)
        assert "   ord 3" not in mini(acc)


class TestUnpinnedRendersIdenticallyToPrOne:
    """AC-39, second half — the goldens, unchanged.

    These are the same literals `test_tui_policy_display.py` pins for PR 1. If
    the new badge branch fires for an unpinned account, or the helper's return
    value gains a stray separator, this fails before any human notices the row
    shifted.
    """

    def test_the_card_header_is_the_pr_one_golden(self):
        acc = with_policy(make_account(2, active=True, alias="dev"))
        assert card(acc).splitlines()[0] == GOLDEN_CARD_HEADER

    def test_the_mini_row_is_the_pr_one_golden(self):
        acc = with_policy(make_account(2, active=True, alias="dev"))
        assert mini(acc) == GOLDEN_MINI

    def test_an_account_with_no_policy_object_at_all_is_unchanged(self):
        acc = make_account(2, active=True, alias="dev")
        assert card(acc).splitlines()[0] == GOLDEN_CARD_HEADER
        assert mini(acc) == GOLDEN_MINI


class TestBadgeOrderIsDeterministic:
    """AC-40 — `(backup)`, then `th NN%`, then `ord R`, always."""

    ALL_THREE = dict(backup=True, threshold=82.5, order=3)

    def test_the_card_renders_all_three_in_order(self):
        acc = with_policy(make_account(2, active=True, alias="dev"), **self.ALL_THREE)
        header = card(acc).splitlines()[0]
        assert header.index("(backup)") < header.index("th 82.5%") < header.index("ord 3")

    def test_the_mini_row_renders_all_three_in_order(self):
        acc = with_policy(make_account(2, active=True, alias="dev"), **self.ALL_THREE)
        row = mini(acc)
        assert row.index("(backup)") < row.index("th 82.5%") < row.index("ord 3")

    def test_the_exact_rendered_badge_run_is_pinned(self):
        """A substring-order check passes on a row that lost its separators.

        The literal run is asserted on the mini path, whose gap is two spaces
        and whose whole line is short enough to pin without a wall clock in it.
        """
        acc = with_policy(make_account(2, active=True, alias="dev"), **self.ALL_THREE)
        assert "  (backup)  th 82.5%  ord 3" in mini(acc)

    def test_order_still_sorts_last_when_the_threshold_is_absent(self):
        acc = with_policy(
            make_account(2, active=True, alias="dev"), backup=True, order=3
        )
        row = mini(acc)
        assert row.index("(backup)") < row.index("ord 3")
        assert "th " not in row

    def test_the_disabled_badge_still_precedes_them_all(self):
        """`(disabled)` is emitted by the render path itself, not the helper.

        Its position relative to the policy badges is therefore not something
        `_policy_badges` controls — which is exactly why it is worth pinning.
        """
        acc = with_policy(
            make_account(3, disabled=True), backup=True, threshold=82.5, order=3
        )
        row = mini(acc)
        assert row.index("(disabled)") < row.index("(backup)") < row.index("ord 3")


class TestTheFailbackReasonReachesTheTui:
    """AC-41 — MEU-ORD-04's reason renders through the existing surface."""

    def test_the_event_log_line_names_the_reason(self):
        e = NoSwitchEvent(reason="failback-hold", detail="primaries still exhausted")
        assert "failback-hold" in event_text(e).plain

    def test_the_log_line_carries_the_detail_too(self):
        e = NoSwitchEvent(reason="failback-hold", detail="primaries still exhausted")
        assert "primaries still exhausted" in event_text(e).plain

    def test_the_json_payload_the_notification_reads_carries_it(self):
        """`app.py:276` reads `payload["reason"]` from the event's own fields.

        Asserting on `_fields()` rather than driving the Textual app keeps this
        a unit test while still pinning the exact key that surface consumes.
        """
        e = NoSwitchEvent(reason="failback-hold", detail="primaries still exhausted")
        assert e._fields()["reason"] == "failback-hold"

    def test_no_special_casing_was_added_for_it(self):
        """AC-41 is met by the generic path, and must stay that way.

        `event_text` and the notification both render whatever string the
        engine emits. A hard-coded `failback-hold` branch anywhere in the TUI
        would be a new maintenance burden for zero behaviour, and would silently
        diverge the moment the engine's wording changes.
        """
        for name in ("autoview.py", "app.py", "widgets.py", "dashboard.py"):
            src = (SRC / "tui" / name).read_text(encoding="utf-8")
            assert "failback-hold" not in src

    def test_the_ordinary_reason_renders_exactly_as_before(self):
        e = NoSwitchEvent(reason="below-threshold", detail="20% < 90%")
        assert event_text(e).plain.endswith("no switch: below-threshold (20% < 90%)")

    def test_a_reasonless_event_still_renders(self):
        e = NoSwitchEvent(reason="failback-hold")
        assert event_text(e).plain.endswith("no switch: failback-hold")


class TestNoTuiEditing:
    """AC-42 — authoring stays CLI-only; this PR adds no input path."""

    EDITABLE = ("modals.py", "dashboard.py")

    @pytest.mark.parametrize("name", EDITABLE)
    def test_the_editing_surfaces_never_mention_order(self, name: str):
        """`order` as a *policy* word, not the English word.

        Matched as `set_account_order` / `account_orders` / `acc.policy.order`,
        so an unrelated comment using "order" in prose does not fail this.
        """
        src = (SRC / "tui" / name).read_text(encoding="utf-8")
        assert not re.search(r"\bset_account_order\b", src)
        assert not re.search(r"\baccount_orders\b", src)
        assert not re.search(r"policy\.order\b", src)

    @pytest.mark.parametrize("name", EDITABLE)
    def test_no_new_order_keybinding(self, name: str):
        src = (SRC / "tui" / name).read_text(encoding="utf-8")
        for m in re.finditer(r'Binding\(\s*"([^"]+)"\s*,\s*"([^"]+)"', src):
            assert "order" not in m.group(2).lower()

    def test_widgets_reads_the_policy_but_never_writes_it(self):
        """The one file that *does* gain `order` may only display it."""
        src = (SRC / "tui" / "widgets.py").read_text(encoding="utf-8")
        assert "policy.order" in src
        assert "set_account_order" not in src

    def test_the_badge_helper_is_still_the_only_extension_point(self):
        """AC-39 asked for the badge in both paths via `_policy_badges`.

        A second, parallel branch inside `account_card_text` would satisfy
        every rendering assertion above and quietly undo the reason PR 1
        extracted the helper in the first place.

        Narrowing: the Red phase spelled this `src.count("policy.order") == 1`,
        which no correct implementation can satisfy -- the idiomatic guard-plus-
        use branch reads the attribute twice, and PR 1's own `policy.threshold`
        counts 2 in this same file for exactly that reason. Rebound to the
        intent: every read of `policy.order` must fall inside `_policy_badges`.
        """
        src = (SRC / "tui" / "widgets.py").read_text(encoding="utf-8")
        lines = src.splitlines()
        start = next(
            i for i, ln in enumerate(lines) if ln.startswith("def _policy_badges(")
        )
        end = next(
            (
                i
                for i, ln in enumerate(lines[start + 1 :], start + 1)
                if ln and not ln[0].isspace()
            ),
            len(lines),
        )
        hits = [i for i, ln in enumerate(lines) if "policy.order" in ln]
        assert hits, "widgets.py never reads policy.order"
        outside = [i + 1 for i in hits if not (start <= i < end)]
        assert not outside, (
            f"policy.order read outside _policy_badges at lines {outside}"
        )
