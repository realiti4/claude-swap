"""Per-account policy value objects and threshold normalization (models.py).

MEU-PAP-01 — AC-1 … AC-5 of
`.agent/plans/2026-09-02-per-account-threshold-backup/implementation-plan.md`.

`models.py` is a leaf module and must not import `settings` (AGENTS.md
§Architecture). The threshold bounds are therefore *declared* in `models.py` and
*cross-checked* against `SETTING_SPECS` here, in the test layer, where importing
both is allowed. AC-2 and AC-5 are the two halves of that arrangement.
"""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path

import pytest

from claude_swap.models import (
    ACCOUNT_THRESHOLD_MAX,
    ACCOUNT_THRESHOLD_MIN,
    AccountPolicy,
    AccountSnapshot,
    normalize_account_threshold,
)
from claude_swap.settings import SETTING_SPECS
from claude_swap.usage_store import UsageEntry


def _snapshot(**overrides) -> AccountSnapshot:
    """An AccountSnapshot built the way every pre-change caller builds one.

    Deliberately passes no ``policy=``: AC-4 is precisely the claim that adding
    the field does not break existing construction sites.
    """
    fields = {
        "number": "1",
        "email": "account-1@example.test",
        "org_name": "Example Org",
        "org_uuid": "00000000-0000-4000-8000-000000000001",
        "is_active": True,
        "kind": "oauth",
        "switchable": True,
        "usage": UsageEntry(),
    }
    fields.update(overrides)
    return AccountSnapshot(**fields)


class TestNormalizeAccountThreshold:
    """AC-1 — the write-boundary validator for a per-account threshold."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (50, 50.0),
            (99, 99.0),
            (85, 85.0),
            (85.5, 85.5),
            (50.0, 50.0),
            (99.9, 99.9),
            ("85", 85.0),
            ("85.5", 85.5),
            ("  85.5  ", 85.5),
            ("50", 50.0),
            ("99.9", 99.9),
        ],
    )
    def test_accepts_int_float_and_numeric_str_in_range(self, raw, expected):
        result = normalize_account_threshold(raw)
        assert result == pytest.approx(expected)
        assert isinstance(result, float), "must return a float regardless of input type"

    @pytest.mark.parametrize("raw", [20, 49.9, 100, 99.91, 0, -85, 1000])
    def test_rejects_out_of_range(self, raw):
        with pytest.raises(ValueError):
            normalize_account_threshold(raw)

    @pytest.mark.parametrize("raw", ["abc", "", "  ", "85%", "eighty-five", None, [], {}, True])
    def test_rejects_non_numeric(self, raw):
        with pytest.raises(ValueError):
            normalize_account_threshold(raw)

    @pytest.mark.parametrize(
        "raw", [float("nan"), float("inf"), float("-inf"), "nan", "inf", "-inf"]
    )
    def test_rejects_nan_and_infinity(self, raw):
        """NaN and infinity are floats that pass a naive ``lo <= v <= hi`` test.

        ``nan`` fails every comparison, so an implementation written as
        ``if not (MIN <= v <= MAX): raise`` rejects it by accident; one written
        as ``if v < MIN or v > MAX: raise`` lets it through. Infinity is the
        mirror case. Both must raise.
        """
        with pytest.raises(ValueError):
            normalize_account_threshold(raw)

    @pytest.mark.parametrize("raw", [20, 100, "abc", float("nan"), float("inf")])
    def test_error_message_names_the_valid_range(self, raw):
        """The message must name the range — a bare 'invalid value' is not enough.

        This is what `cswap threshold 2 20` prints to the user (AC-38).
        """
        with pytest.raises(ValueError) as excinfo:
            normalize_account_threshold(raw)
        message = str(excinfo.value)
        assert "50" in message, f"message must name the lower bound: {message!r}"
        assert "99.9" in message, f"message must name the upper bound: {message!r}"

    def test_boundaries_are_inclusive(self):
        assert normalize_account_threshold(ACCOUNT_THRESHOLD_MIN) == ACCOUNT_THRESHOLD_MIN
        assert normalize_account_threshold(ACCOUNT_THRESHOLD_MAX) == ACCOUNT_THRESHOLD_MAX

    def test_bool_is_not_a_number_here(self):
        """``True`` is an ``int`` in Python and would normalize to 1.0.

        1.0 is out of range so a naive implementation raises anyway, but for the
        right reason only by luck. Pinned so the behaviour is deliberate.
        """
        with pytest.raises(ValueError):
            normalize_account_threshold(True)


class TestThresholdBoundsMatchSettings:
    """AC-2 — the bounds are declared in `models.py` but owned by `SETTING_SPECS`."""

    def test_constants_have_the_documented_values(self):
        assert ACCOUNT_THRESHOLD_MIN == 50.0
        assert ACCOUNT_THRESHOLD_MAX == 99.9

    def test_constants_equal_the_global_threshold_spec_bounds(self):
        """Drift in either direction fails.

        A per-account threshold that could be set outside the range the global
        setting accepts would be a second, silently different validity rule.
        """
        spec = SETTING_SPECS["autoswitch.threshold"]
        assert ACCOUNT_THRESHOLD_MIN == spec.lo
        assert ACCOUNT_THRESHOLD_MAX == spec.hi

    def test_constants_are_floats(self):
        assert isinstance(ACCOUNT_THRESHOLD_MIN, float)
        assert isinstance(ACCOUNT_THRESHOLD_MAX, float)


class TestAccountPolicy:
    """AC-3 — the value object carried on every snapshot row."""

    def test_default_is_empty(self):
        policy = AccountPolicy()
        assert policy.threshold is None
        assert policy.backup is False

    def test_two_defaults_compare_equal(self):
        """Equality is what makes the omit-when-default record convention testable.

        AC-10 asserts an untouched record is unchanged; that check is written in
        terms of policy equality, so it must be value equality, not identity.
        """
        assert AccountPolicy() == AccountPolicy()
        assert AccountPolicy(threshold=85.0) == AccountPolicy(threshold=85.0)
        assert AccountPolicy(threshold=85.0) != AccountPolicy(threshold=90.0)
        assert AccountPolicy(backup=True) != AccountPolicy()

    def test_is_frozen(self):
        """`AccountSnapshot` is `frozen=True`; a mutable member would break that."""
        policy = AccountPolicy()
        with pytest.raises(dataclasses.FrozenInstanceError):
            policy.threshold = 85.0  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            policy.backup = True  # type: ignore[misc]

    def test_is_hashable(self):
        """A frozen dataclass with no `eq=False` is hashable; pin it.

        `AccountsSnapshot.accounts` is a tuple of frozen rows, and callers are
        entitled to put a snapshot row in a set.
        """
        assert len({AccountPolicy(), AccountPolicy()}) == 1

    def test_field_types_are_declared_as_specified(self):
        """A closed-world fence on the field set.

        Written for PR 1 as ``{"threshold", "backup"}`` with the note that
        ``order`` belonged to PR 2 and must not appear early. PR 2 (MEU-ORD-01,
        AC-5) is that event, so the fence moves rather than being deleted — its
        job is to make any *further* field an explicit decision, and PR 2's own
        ``tests/test_account_order_values.py`` asserts the same set from the
        other side.
        """
        fields = {f.name: f for f in dataclasses.fields(AccountPolicy)}
        assert set(fields) == {"threshold", "backup", "order"}, (
            "the field set is fixed; a new field is a deliberate, tested addition"
        )
        assert fields["threshold"].default is None
        assert fields["backup"].default is False
        assert fields["order"].default is None


class TestAccountSnapshotPolicyField:
    """AC-4 — adding the field must not break any existing construction site."""

    def test_construction_without_policy_yields_the_default(self):
        snapshot = _snapshot()
        assert snapshot.policy == AccountPolicy()

    def test_policy_can_be_supplied(self):
        policy = AccountPolicy(threshold=85.0, backup=True)
        assert _snapshot(policy=policy).policy == policy

    def test_policy_is_last_so_positional_callers_are_unaffected(self):
        """The field is appended after `disabled`, not inserted.

        Inserting it ahead of `alias`/`disabled` would silently rebind every
        positional construction in the repo.
        """
        names = [f.name for f in dataclasses.fields(AccountSnapshot)]
        assert names[-1] == "policy"
        assert names.index("policy") > names.index("disabled")

    def test_snapshot_is_still_frozen_and_still_has_display_tag(self):
        snapshot = _snapshot(policy=AccountPolicy(backup=True))
        with pytest.raises(dataclasses.FrozenInstanceError):
            snapshot.policy = AccountPolicy()  # type: ignore[misc]
        assert snapshot.display_tag == "Example Org"

    def test_every_existing_construction_site_still_works(self):
        """The repo's own callers are the real regression surface.

        `switcher.py` builds these rows at two sites and the TUI builds them in
        fixtures; none pass `policy=`. If the field had no default, or were not
        last, this import-and-build would fail.
        """
        rows = (
            _snapshot(number="1", is_active=True),
            _snapshot(number="2", is_active=False, kind="api_key", switchable=False),
            _snapshot(number="3", is_active=False, alias="work", disabled=True),
        )
        assert all(row.policy == AccountPolicy() for row in rows)


class TestModelsRemainsALeaf:
    """AC-5 — `models.py` must not import `settings`.

    The dependency direction is presentation → policy → store → platform I/O →
    leaf data types (AGENTS.md §Architecture). `settings` sits above `models`;
    an import here would make the leaf cyclic and is the reason AC-2's bounds
    are cross-checked in the test layer instead.
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
        assert not hasattr(models_module, "AutoSwitchSettings")
