"""Per-account chain order value object and normalization (models.py).

MEU-ORD-01 — AC-1 … AC-8 of
`.agent/plans/2026-09-03-per-account-order/implementation-plan.md`.

`order` is an **ordinal**, not a percentage: it indexes a chain rather than
measuring one, so unlike the one-decimal float `threshold` it normalizes to an
`int`. Integral input in any numeric form is accepted (`2`, `2.0`, `"2"` all
yield the `int` 2) because the CLI hands over argv strings and a JSON store may
round-trip a whole number as a float; fractional input is rejected, because
there is no such thing as position 1.5 in a chain.

The strict-write / tolerant-read asymmetry is inherited from PR 1: this module
is the single **write**-boundary validator, and `switcher.py` stays forgiving
when reading a store some future version may have written.
"""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path

import pytest

from claude_swap.models import (
    ACCOUNT_ORDER_MAX,
    ACCOUNT_ORDER_MIN,
    ORDER_UNSET_RANK,
    AccountPolicy,
    normalize_account_order,
)


class TestOrderConstants:
    """AC-1, AC-6 — the bounds and the unset sentinel."""

    def test_constants_have_the_documented_values(self):
        assert ACCOUNT_ORDER_MIN == 1
        assert ACCOUNT_ORDER_MAX == 999

    def test_constants_are_ints(self):
        """Not floats. An ordinal that arrives as 1.0 would print as '1.0'."""
        assert type(ACCOUNT_ORDER_MIN) is int
        assert type(ACCOUNT_ORDER_MAX) is int
        assert type(ORDER_UNSET_RANK) is int

    def test_lower_bound_is_one_not_zero(self):
        """0 is held back as a non-value so a future 'always first' sentinel
        has room (plan §Spec Sufficiency, Research-backed)."""
        assert ACCOUNT_ORDER_MIN == 1

    def test_unset_rank_outranks_every_pinned_value(self):
        """AC-6 — the assertion *is* the test.

        `OrderMap.__missing__` returns `ORDER_UNSET_RANK`, and the tier is the
        leading element of the sort key, which sorts ascending. If the sentinel
        were not strictly greater than every legal pin, an unpinned account
        could outrank a pinned one and the whole feature would be unsound.
        """
        assert ORDER_UNSET_RANK > ACCOUNT_ORDER_MAX


class TestNormalizeAccountOrder:
    """AC-1, AC-3, AC-4 — the write-boundary validator."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            (1, 1),
            (999, 999),
            (7, 7),
            (2.0, 2),          # AC-4: integral float accepted
            (999.0, 999),
            ("2", 2),          # AC-3: the CLI hands over argv strings
            ("  5  ", 5),      # AC-3: whitespace stripped
            ("1", 1),
            ("999", 999),
            ("2.0", 2),        # AC-4: integral numeric str accepted
        ],
    )
    def test_accepts_integral_values_in_range(self, raw, expected):
        result = normalize_account_order(raw)
        assert result == expected
        assert type(result) is int, "order is an ordinal and must normalize to int"

    @pytest.mark.parametrize("raw", [0, -1, 1000, "0", "-1", "1000", 0.0, 1000.0])
    def test_rejects_out_of_range(self, raw):
        with pytest.raises(ValueError):
            normalize_account_order(raw)

    @pytest.mark.parametrize("raw", [0, -1, 1000, 1.5, "abc", None])
    def test_error_message_names_the_valid_range(self, raw):
        """Every rejection path names the range, as `normalize_account_threshold`
        does — a user who typed 1000 learns what is legal without reading docs."""
        with pytest.raises(ValueError) as excinfo:
            normalize_account_order(raw)
        message = str(excinfo.value)
        assert "1" in message and "999" in message

    def test_boundaries_are_inclusive(self):
        assert normalize_account_order(ACCOUNT_ORDER_MIN) == ACCOUNT_ORDER_MIN
        assert normalize_account_order(ACCOUNT_ORDER_MAX) == ACCOUNT_ORDER_MAX

    @pytest.mark.parametrize("raw", ["abc", "", "   ", "1x", "x1", "1 2", None, [], {}, object()])
    def test_rejects_non_numeric(self, raw):
        with pytest.raises(ValueError):
            normalize_account_order(raw)

    @pytest.mark.parametrize("raw", ["abc", "", "1x"])
    def test_non_numeric_message_quotes_the_value(self, raw):
        """AC-3 — quoted via !r, so an empty string is visible in the error."""
        with pytest.raises(ValueError) as excinfo:
            normalize_account_order(raw)
        assert repr(raw) in str(excinfo.value)


class TestFractionalAndSpecialValues:
    """AC-4 — the discriminating case for the whole MEU."""

    @pytest.mark.parametrize("raw", [1.5, 2.5, 0.5, 998.5, "1.5", "2.5", " 3.75 "])
    def test_rejects_fractional(self, raw):
        """There is no position 1.5 in a chain.

        This is the pair to `test_accepts_integral_values_in_range`: both forms
        of 2 are accepted, no form of 1.5 is. Accepting `2.0` is not laxity —
        it is the recognition that a JSON round-trip can widen an int, while
        rejecting 1.5 is the actual ordinal constraint.
        """
        with pytest.raises(ValueError):
            normalize_account_order(raw)

    @pytest.mark.parametrize("raw", [1.5, "1.5"])
    def test_fractional_message_explains_why(self, raw):
        """A range-only message would be actively misleading for 1.5, which is
        inside 1…999. The rejection reason is integrality, and the message says so."""
        with pytest.raises(ValueError) as excinfo:
            normalize_account_order(raw)
        assert "whole number" in str(excinfo.value).lower()

    @pytest.mark.parametrize(
        "raw",
        [float("nan"), float("inf"), float("-inf"), "nan", "inf", "-inf", "NaN", "Infinity"],
    )
    def test_rejects_nan_and_infinity(self, raw):
        """Rejected *by name*, before the range test.

        NaN fails every comparison (so `not MIN <= nan <= MAX` is True by luck,
        not by design) and `int(inf)` raises `OverflowError`, not `ValueError`.
        Both would escape or mis-raise if left to the range test.
        """
        with pytest.raises(ValueError):
            normalize_account_order(raw)

    @pytest.mark.parametrize("raw", [True, False])
    def test_bool_is_rejected_by_name(self, raw):
        """AC-2 — `True` is an `int` in Python and would normalize to 1, which
        is a *legal* order. It would be silently accepted, not merely mis-erred."""
        with pytest.raises(ValueError) as excinfo:
            normalize_account_order(raw)
        assert repr(raw) in str(excinfo.value), (
            "the message must quote True/False, so the user sees what they passed"
        )

    @pytest.mark.parametrize("raw", [True, False])
    def test_bool_never_normalizes_successfully(self, raw):
        """The failure mode being guarded is silent *acceptance*: without the
        explicit `isinstance(value, bool)` guard, `True` reaches the range test
        as 1, which is a legal order, and is stored as account priority 1."""
        with pytest.raises(ValueError):
            normalize_account_order(raw)


class TestAccountPolicyOrderField:
    """AC-5, AC-8 — the field on the frozen dataclass."""

    def test_default_is_none(self):
        assert AccountPolicy().order is None

    def test_order_can_be_supplied(self):
        assert AccountPolicy(order=3).order == 3

    def test_full_default_identity(self):
        """AC-8 — the whole field tuple, not just `order`.

        Asserting every field means a future field added with a truthy default
        fails *here*, at the one place that claims 'a fleet with no policy set
        behaves exactly as it did before'.
        """
        policy = AccountPolicy()
        assert policy.threshold is None
        assert policy.backup is False
        assert policy.order is None
        assert dataclasses.astuple(policy) == (None, False, None)

    def test_two_defaults_compare_equal(self):
        assert AccountPolicy() == AccountPolicy()

    def test_is_still_frozen(self):
        """AC-5 — `AccountSnapshot` is frozen; a mutable member would silently
        break that guarantee for the whole row."""
        policy = AccountPolicy(order=2)
        with pytest.raises(dataclasses.FrozenInstanceError):
            policy.order = 5  # type: ignore[misc]

    def test_is_still_hashable(self):
        assert hash(AccountPolicy(order=2)) == hash(AccountPolicy(order=2))
        assert hash(AccountPolicy(order=2)) != hash(AccountPolicy(order=3))

    def test_order_field_is_declared_as_specified(self):
        fields = {f.name: f for f in dataclasses.fields(AccountPolicy)}
        assert "order" in fields
        assert fields["order"].type in ("int | None", "int|None")
        assert fields["order"].default is None

    def test_order_is_last_so_positional_callers_are_unaffected(self):
        """PR 1 built `AccountPolicy(threshold, backup)` positionally in places.
        Appending rather than inserting keeps every one of those sites correct."""
        names = [f.name for f in dataclasses.fields(AccountPolicy)]
        assert names == ["threshold", "backup", "order"]
        assert AccountPolicy(85.0, True, 2) == AccountPolicy(
            threshold=85.0, backup=True, order=2
        )


class TestModelsRemainsALeaf:
    """AC-7 — `models.py` must not import `settings` (AGENTS.md §Architecture).

    The dependency direction is presentation → policy → store → platform I/O →
    leaf data types. Unlike `threshold`, whose bounds mirror a `SETTING_SPECS`
    entry, `order` has no global counterpart to drift against — but the leaf
    invariant is a property of the module, so adding a field is exactly when it
    is worth re-asserting.
    """

    def test_no_settings_import_in_source(self):
        source = Path("src/claude_swap/models.py").read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in source.splitlines()
            if ("import" in line and "settings" in line and not line.strip().startswith("#"))
        ]
        assert offenders == [], (
            "models.py must not import settings (AGENTS.md §Architecture): "
            f"{offenders}"
        )

    def test_models_module_has_no_settings_attribute(self):
        """The source scan above misses `importlib`-style indirection."""
        import claude_swap.models as models_module

        assert not hasattr(models_module, "settings")
        assert not hasattr(models_module, "SETTING_SPECS")

    def test_order_bounds_are_module_literals(self):
        """The bounds are declared here, not derived from a settings lookup —
        the mechanism by which the leaf invariant is kept."""
        source = Path("src/claude_swap/models.py").read_text(encoding="utf-8")
        assert "ACCOUNT_ORDER_MIN = 1" in source
        assert "ACCOUNT_ORDER_MAX = 999" in source
