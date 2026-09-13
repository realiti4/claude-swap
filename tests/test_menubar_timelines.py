"""Contract tests for the additive ``timelineWindows`` view-model.

The reset-timelines charts need richer states than ``windows[]`` carries:
known-reset/missing-usage, elapsed resets with retained timestamps, and
per-account staleness. ``timelineWindows`` is additive — ``windows[]`` and
the quota cards must stay byte-identical (that half is asserted here too).

Wire note: inside ``timelineWindows`` the five fields are ALWAYS present,
``None`` meaning unknown — the chart must distinguish unknown from absent,
and null is never zero. This is a deliberate, documented deviation from
the vm-wide absent-when-optional convention, pinned here.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import pytest

from claude_swap.json_output import USAGE_API_KEY
from claude_swap.menubar import viewmodel
from claude_swap.models import AccountsSnapshot
from claude_swap.usage_store import UsageEntry

from tests.test_menubar_viewmodel import NOW, _iso, _win, account, usage_fixture

TL_FIELDS = {"kind", "pct", "resetsAt", "startsAt", "state", "observedAt"}


def build_snapshot(*accounts) -> AccountsSnapshot:
    return AccountsSnapshot(
        accounts=list(accounts), taken_at=NOW - 120, active_number="1"
    )


def tl_of(vm: dict, kind: str) -> dict:
    entries = [w for w in vm["accounts"][0]["timelineWindows"] if w["kind"] == kind]
    assert len(entries) == 1, f"expected exactly one {kind} entry"
    return entries[0]


def first_vm(*accounts) -> dict:
    return viewmodel.build(build_snapshot(*accounts), now=NOW)


class TestHealthy:
    def test_both_kinds_emitted_ok_with_explicit_nullables(self):
        vm = first_vm(account())
        tl = vm["accounts"][0]["timelineWindows"]
        assert [w["kind"] for w in tl] == ["5h", "7d"]
        for w in tl:
            assert set(w) == TL_FIELDS
            assert w["state"] == "ok"
            assert w["startsAt"] is None  # no measured start in the model yet
            assert w["observedAt"] == NOW - 120
        assert tl_of(vm, "5h")["pct"] == 68.4
        assert tl_of(vm, "5h")["resetsAt"] == NOW + 8040

    def test_over_100_pct_is_retained_uncapped(self):
        usage = usage_fixture()
        usage["five_hour"]["pct"] = 115.0
        vm = first_vm(account(usage=UsageEntry(last_good=usage, fetched_at=NOW)))
        assert tl_of(vm, "5h")["pct"] == 115.0
        assert tl_of(vm, "5h")["state"] == "ok"

    def test_windows_and_quota_output_unchanged(self):
        """Additive only: windows[]/spend keep their exact prior shape."""
        vm = first_vm(account())
        acc = vm["accounts"][0]
        assert [w["kind"] for w in acc["windows"]] == ["5h", "7d", "model:Fable"]
        assert acc["spend"]["pct"] == 12.4
        # timelineWindows excludes scoped/model and spend kinds
        assert {w["kind"] for w in acc["timelineWindows"]} == {"5h", "7d"}


class TestPrecedence:
    def test_elapsed_reset_retains_timestamp_and_pct(self):
        usage = usage_fixture()
        usage["seven_day"]["resets_at"] = _iso(NOW - 3600)
        vm = first_vm(account(usage=UsageEntry(last_good=usage, fetched_at=NOW)))
        w = tl_of(vm, "7d")
        assert w["state"] == "elapsed"
        assert w["resetsAt"] == NOW - 3600  # kept, unlike windows[] which drops it
        assert w["pct"] == 41.0  # measured value, never fabricated to zero

    def test_exact_expiry_is_elapsed(self):
        usage = usage_fixture()
        usage["five_hour"]["resets_at"] = _iso(NOW)
        vm = first_vm(account(usage=UsageEntry(last_good=usage, fetched_at=NOW)))
        assert tl_of(vm, "5h")["state"] == "elapsed"

    def test_missing_usage_future_reset_is_usage_unavailable(self):
        usage = usage_fixture()
        del usage["five_hour"]["pct"]
        vm = first_vm(account(usage=UsageEntry(last_good=usage, fetched_at=NOW)))
        w = tl_of(vm, "5h")
        assert w["state"] == "usage-unavailable"
        assert w["pct"] is None
        assert w["resetsAt"] == NOW + 8040

    def test_missing_reset_is_reset_unavailable(self):
        usage = usage_fixture()
        del usage["five_hour"]["resets_at"]
        vm = first_vm(account(usage=UsageEntry(last_good=usage, fetched_at=NOW)))
        w = tl_of(vm, "5h")
        assert w["state"] == "reset-unavailable"
        assert w["resetsAt"] is None
        assert w["pct"] == 68.4  # known usage still shown in detail

    def test_nonfinite_or_negative_pct_is_missing(self):
        for bad in (float("nan"), float("inf"), -3.0):
            usage = usage_fixture()
            usage["five_hour"]["pct"] = bad
            vm = first_vm(account(usage=UsageEntry(last_good=usage, fetched_at=NOW)))
            w = tl_of(vm, "5h")
            assert w["pct"] is None, f"{bad} must not reach the wire"
            assert w["state"] == "usage-unavailable"
        assert "NaN" not in json.dumps(vm)
        assert "Infinity" not in json.dumps(vm)

    def test_elapsed_wins_over_stale(self):
        """DATA-CONTRACT: reset <= N is 'awaiting updated usage ... even if
        marked stale'."""
        usage = usage_fixture()
        usage["five_hour"]["resets_at"] = _iso(NOW - 60)
        vm = first_vm(account(usage=UsageEntry(
            last_good=usage, fetched_at=NOW, last_error="boom")))
        assert tl_of(vm, "5h")["state"] == "elapsed"

    def test_stale_future_reset_is_stale_per_account(self):
        healthy = account(number="2", is_active=False)
        stale = account(
            number="1",
            usage=UsageEntry(
                last_good=usage_fixture(), fetched_at=NOW, last_error="net down"),
        )
        vm = viewmodel.build(build_snapshot(stale, healthy), now=NOW)
        by_slot = {a["slot"]: a for a in vm["accounts"]}
        assert tl_of_slot(by_slot["1"], "5h")["state"] == "stale"
        assert tl_of_slot(by_slot["2"], "5h")["state"] == "ok"  # not global


def tl_of_slot(acc_vm: dict, kind: str) -> dict:
    entries = [w for w in acc_vm["timelineWindows"] if w["kind"] == kind]
    assert len(entries) == 1
    return entries[0]


class TestAccountLevel:
    def test_sentinel_account_emits_no_window_both_kinds(self):
        acc = account(usage=UsageEntry(
            sentinel=USAGE_API_KEY, last_good=None, fetched_at=NOW))
        vm = first_vm(acc)
        for kind in ("5h", "7d"):
            w = tl_of(vm, kind)
            assert w["state"] == "no-window"
            assert w["pct"] is None and w["resetsAt"] is None

    def test_error_without_measurement_is_unavailable(self):
        acc = account(usage=UsageEntry(
            last_error="no route", last_good=None, fetched_at=None))
        vm = first_vm(acc)
        for kind in ("5h", "7d"):
            assert tl_of(vm, kind)["state"] == "unavailable"

    def test_usage_dict_missing_kind_is_no_window_for_that_kind(self):
        usage = usage_fixture()
        del usage["seven_day"]
        vm = first_vm(account(usage=UsageEntry(last_good=usage, fetched_at=NOW)))
        assert tl_of(vm, "5h")["state"] == "ok"
        assert tl_of(vm, "7d")["state"] == "no-window"


class TestSanitization:
    def test_json_serializable_never_nan_or_infinity(self):
        usage = usage_fixture()
        usage["five_hour"]["resets_at"] = "not-a-date"  # unparseable -> None
        vm = first_vm(account(usage=UsageEntry(last_good=usage, fetched_at=NOW)))
        assert tl_of(vm, "5h")["resetsAt"] is None
        json.dumps(vm)  # raises on NaN/Infinity literals
